from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from falafacil.config import DEFAULT_MODEL
from falafacil.transcription import (
    INLINE_LIMIT_BYTES,
    PROMPT,
    REQUEST_TIMEOUT_MS,
    GeminiTranscriber,
    TokenUsage,
    TranscriptionDebug,
    TranscriptionError,
    TranscriptionWorker,
    _extract_usage,
    _friendly_api_error,
    _to_int,
)


class FakeInteraction:
    def __init__(
        self,
        output_text: str = "  Olá, terminal!  ",
        usage: Any = None,
    ) -> None:
        self.output_text = output_text
        self.usage = usage


class FakeInteractions:
    def __init__(self, interaction: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._interaction = interaction

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._interaction is not None:
            return self._interaction
        return FakeInteraction()


class FakeClient:
    def __init__(self, interaction: Any = None) -> None:
        self.interactions = FakeInteractions(interaction=interaction)


def test_transcriber_sends_inline_wav_and_returns_trimmed_text() -> None:
    client = FakeClient()
    transcriber = GeminiTranscriber(client=client)
    audio = b"RIFFfake-wav"

    result = transcriber.transcribe(audio)

    assert result == "Olá, terminal!"
    assert len(client.interactions.calls) == 1
    call = client.interactions.calls[0]
    assert set(call.keys()) == {"model", "input"}
    assert "cached_content" not in call
    assert "previous_interaction_id" not in call
    assert "file" not in call
    assert "files" not in call
    assert "batch" not in call
    assert call["model"] == DEFAULT_MODEL
    assert len(call["input"]) == 2
    assert call["input"][0] == {"type": "text", "text": PROMPT}
    assert "português do Brasil" in call["input"][0]["text"]
    audio_part = call["input"][1]
    assert audio_part["type"] == "audio"
    assert audio_part["mime_type"] == "audio/wav"
    assert base64.b64decode(audio_part["data"]) == audio
    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.model == DEFAULT_MODEL
    assert debug.prompt == PROMPT
    assert debug.audio_bytes == len(audio)
    assert debug.audio_mime_type == "audio/wav"
    assert debug.audio_base64_length == len(audio_part["data"])
    assert debug.audio_base64_preview == audio_part["data"]
    assert debug.response_text == "Olá, terminal!"
    assert debug.error is None


def test_transcriber_limits_debug_preview_for_oversized_audio() -> None:
    transcriber = GeminiTranscriber(client=FakeClient())

    with pytest.raises(TranscriptionError, match="longa demais"):
        transcriber.transcribe(b"x" * (INLINE_LIMIT_BYTES + 1))

    debug = transcriber.last_debug()
    assert debug is not None
    assert len(debug.audio_base64_preview) <= 128
    assert debug.audio_base64_length > len(debug.audio_base64_preview)
    assert debug.error is not None


def test_transcriber_debug_records_empty_response_error() -> None:
    client = FakeClient()
    client.interactions.create = lambda **kwargs: type(
        "Response", (), {"output_text": "  "}
    )()
    transcriber = GeminiTranscriber(client=client)

    with pytest.raises(TranscriptionError, match="não retornou texto"):
        transcriber.transcribe(b"audio")

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.response_text == ""
    assert "não retornou texto" in (debug.error or "")


def test_transcriber_trace_redacts_injected_api_key_from_api_error() -> None:
    client = FakeClient()
    client.interactions.create = lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError("request failed synthetic-client-token")
    )
    transcriber = GeminiTranscriber(
        client=client,
        api_key="synthetic-client-token",
    )

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(b"audio")

    assert str(exc_info.value) == "Não foi possível transcrever o áudio."
    assert "synthetic-client-token" not in str(exc_info.value)
    assert "request failed" not in str(exc_info.value)

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.error == "Não foi possível transcrever o áudio."
    assert "synthetic-client-token" not in (debug.error or "")
    assert "request failed" not in (debug.error or "")


def test_unclassified_api_error_does_not_leak_raw_exception_or_secret() -> None:
    secret = "synthetic-secret-token-xyz987"
    raw_error_text = f"Connection reset by peer at https://internal.service/?token={secret}"
    client = FakeClient()
    client.interactions.create = lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError(raw_error_text)
    )
    transcriber = GeminiTranscriber(
        client=client,
        api_key=secret,
    )

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.transcribe(b"RIFFfake-wav")

    err_msg = str(exc_info.value)
    assert err_msg == "Não foi possível transcrever o áudio."
    assert secret not in err_msg
    assert "Connection reset" not in err_msg
    assert "internal.service" not in err_msg

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.error == "Não foi possível transcrever o áudio."
    assert secret not in (debug.error or "")
    assert "Connection reset" not in (debug.error or "")
    assert "internal.service" not in (debug.error or "")

    QApplication.instance() or QApplication([])
    worker = TranscriptionWorker(transcriber, b"RIFFfake-wav")
    failed_payload: list[tuple[str, Any]] = []
    worker.failed.connect(lambda err, dbg: failed_payload.append((err, dbg)))
    worker.run()

    assert len(failed_payload) == 1
    worker_err, worker_debug = failed_payload[0]
    assert worker_err == "Não foi possível transcrever o áudio."
    assert secret not in worker_err
    assert "Connection reset" not in worker_err
    assert "internal.service" not in worker_err
    assert worker_debug is not None
    assert worker_debug.error == "Não foi possível transcrever o áudio."
    assert secret not in (worker_debug.error or "")
    assert "Connection reset" not in (worker_debug.error or "")
    assert "internal.service" not in (worker_debug.error or "")


@pytest.mark.parametrize(
    ("raw_message", "expected_substring"),
    [
        ("401 Unauthorized", "Chave Gemini inválida"),
        ("authentication failure", "Chave Gemini inválida"),
        ("invalid api key provided", "Chave Gemini inválida"),
        ("404 Not Found", "Modelo Gemini não encontrado"),
        ("model_not_found: gemini-3.7-flash", "Modelo Gemini não encontrado"),
        ("429 Resource Exhausted", "Limite da API Gemini"),
        ("quota exceeded", "Limite da API Gemini"),
        ("rate_limit reached", "Limite da API Gemini"),
        (
            "Error code: 429 - {'error': {'message': 'Your prepayment credits "
            "are depleted. Please go to AI Studio at https://ai.studio/projects "
            "to manage your project and billing.', 'code': "
            "'too_many_requests'}}",
            "Créditos pré-pagos da API Gemini esgotados",
        ),
        (
            "prepayment credits are depleted",
            "Créditos pré-pagos da API Gemini esgotados",
        ),
        ("ReadTimeout", "não respondeu dentro do tempo limite"),
        (
            "The read operation timed out",
            "não respondeu dentro do tempo limite",
        ),
        ("500 Internal Server Error", "O serviço Gemini está indisponível"),
        ("503 Service Unavailable", "O serviço Gemini está indisponível"),
        ("504 Gateway Timeout", "O serviço Gemini está indisponível"),
        ("service unavailable", "O serviço Gemini está indisponível"),
        ("deadline exceeded", "O serviço Gemini está indisponível"),
    ],
)
def test_transcriber_classifies_known_api_errors(
    raw_message: str, expected_substring: str
) -> None:
    client = FakeClient()
    client.interactions.create = lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError(raw_message)
    )
    transcriber = GeminiTranscriber(client=client)
    with pytest.raises(TranscriptionError, match=expected_substring):
        transcriber.transcribe(b"RIFFfake-wav")
    debug = transcriber.last_debug()
    assert debug is not None
    assert expected_substring in (debug.error or "")

def test_worker_emits_text_and_debug_trace() -> None:
    QApplication.instance() or QApplication([])
    transcriber = GeminiTranscriber(client=FakeClient())
    worker = TranscriptionWorker(transcriber, b"audio")
    received = []
    worker.finished.connect(lambda text, debug: received.append((text, debug)))

    worker.run()

    assert received[0][0] == "Olá, terminal!"
    assert received[0][1].response_text == "Olá, terminal!"


def test_transcriber_builds_genai_client_with_api_key(monkeypatch) -> None:
    calls = []

    class ConstructedClient(FakeClient):
        def __init__(self, **kwargs):
            calls.append(kwargs)
            super().__init__()

    monkeypatch.setattr(
        "falafacil.transcription.genai.Client",
        ConstructedClient,
    )

    transcriber = GeminiTranscriber(api_key="synthetic-client-token")

    assert transcriber.client.__class__ is ConstructedClient
    assert calls == [
        {
            "api_key": "synthetic-client-token",
            "http_options": {"timeout": REQUEST_TIMEOUT_MS},
        }
    ]


def test_transcriber_bounds_requests_with_a_positive_timeout(monkeypatch) -> None:
    calls = []

    class ConstructedClient(FakeClient):
        def __init__(self, **kwargs):
            calls.append(kwargs)
            super().__init__()

    monkeypatch.setattr(
        "falafacil.transcription.genai.Client",
        ConstructedClient,
    )

    GeminiTranscriber()

    assert REQUEST_TIMEOUT_MS > 0
    assert calls == [{"http_options": {"timeout": REQUEST_TIMEOUT_MS}}]


def test_transcriber_rejects_empty_and_oversized_audio() -> None:
    transcriber = GeminiTranscriber(client=FakeClient())

    with pytest.raises(TranscriptionError, match="vazio"):
        transcriber.transcribe(b"")
    with pytest.raises(TranscriptionError, match="longa demais"):
        transcriber.transcribe(b"x" * (INLINE_LIMIT_BYTES + 1))


def test_token_usage_dataclass_fields_and_immutability() -> None:
    empty = TokenUsage()
    assert empty.input_tokens is None
    assert empty.output_tokens is None
    assert empty.thought_tokens is None
    assert empty.cached_tokens is None
    assert empty.tool_use_tokens is None
    assert empty.total_tokens is None

    usage = TokenUsage(
        input_tokens=10,
        output_tokens=4,
        thought_tokens=2,
        cached_tokens=1,
        tool_use_tokens=3,
        total_tokens=20,
    )
    assert usage.input_tokens == 10
    assert usage.output_tokens == 4
    assert usage.thought_tokens == 2
    assert usage.cached_tokens == 1
    assert usage.tool_use_tokens == 3
    assert usage.total_tokens == 20

    with pytest.raises(FrozenInstanceError):
        usage.input_tokens = 99  # type: ignore[misc]


def test_transcription_debug_has_optional_usage_field() -> None:
    debug_default = TranscriptionDebug(
        model="model-test",
        prompt="prompt-test",
        audio_bytes=100,
        audio_mime_type="audio/wav",
        audio_base64_length=136,
        audio_base64_preview="preview",
        response_text="text",
        error=None,
    )
    assert debug_default.usage is None

    usage = TokenUsage(input_tokens=5, output_tokens=2, total_tokens=7)
    debug_with_usage = TranscriptionDebug(
        model="model-test",
        prompt="prompt-test",
        audio_bytes=100,
        audio_mime_type="audio/wav",
        audio_base64_length=136,
        audio_base64_preview="preview",
        response_text="text",
        error=None,
        usage=usage,
    )
    assert debug_with_usage.usage == usage


def test_transcriber_extracts_all_six_usage_fields_from_typed_object() -> None:
    class TypedUsage:
        total_input_tokens = 10
        total_output_tokens = 4
        total_thought_tokens = 2
        total_cached_tokens = 1
        total_tool_use_tokens = 3
        total_tokens = 20

    interaction = FakeInteraction(
        output_text="Texto com uso tipado",
        usage=TypedUsage(),
    )
    transcriber = GeminiTranscriber(client=FakeClient(interaction=interaction))

    result = transcriber.transcribe(b"RIFFfake-wav")

    assert result == "Texto com uso tipado"
    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.usage is not None
    assert debug.usage.input_tokens == 10
    assert debug.usage.output_tokens == 4
    assert debug.usage.thought_tokens == 2
    assert debug.usage.cached_tokens == 1
    assert debug.usage.tool_use_tokens == 3
    assert debug.usage.total_tokens == 20


def test_transcriber_extracts_usage_from_dict_and_converts_valid_integers() -> None:
    raw_usage = {
        "total_input_tokens": "12",
        "total_output_tokens": 6,
        "total_tokens": 18,
    }
    interaction = FakeInteraction(
        output_text="Texto com uso em dict",
        usage=raw_usage,
    )
    transcriber = GeminiTranscriber(client=FakeClient(interaction=interaction))

    result = transcriber.transcribe(b"RIFFfake-wav")

    assert result == "Texto com uso em dict"
    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.usage is not None
    assert debug.usage.input_tokens == 12
    assert debug.usage.output_tokens == 6
    assert debug.usage.thought_tokens is None
    assert debug.usage.cached_tokens is None
    assert debug.usage.tool_use_tokens is None
    assert debug.usage.total_tokens == 18


def test_to_int_rejects_floats_negatives_decimal_strings_and_non_integers() -> None:
    # None and booleans
    assert _to_int(None) is None
    assert _to_int(True) is None
    assert _to_int(False) is None

    # Floats (including 1.9, 1.0, 0.0, negatives)
    assert _to_int(1.9) is None
    assert _to_int(1.0) is None
    assert _to_int(0.0) is None
    assert _to_int(-1.5) is None
    assert _to_int(-0.1) is None

    # Negative integers
    assert _to_int(-1) is None
    assert _to_int(-100) is None

    # Decimal and negative strings
    assert _to_int("1.9") is None
    assert _to_int("1.0") is None
    assert _to_int("-1") is None
    assert _to_int("-100") is None
    assert _to_int("1e3") is None

    # Empty and invalid strings or types
    assert _to_int("") is None
    assert _to_int("   ") is None
    assert _to_int("not-an-int") is None
    assert _to_int([]) is None
    assert _to_int({}) is None

    # Valid non-negative integers (int and numeric strings)
    assert _to_int(0) == 0
    assert _to_int(1) == 1
    assert _to_int(42) == 42
    assert _to_int("0") == 0
    assert _to_int("12") == 12
    assert _to_int(" 100 ") == 100


def test_transcriber_handles_empty_absent_and_invalid_usage() -> None:
    # Case 1: usage is None
    t1 = GeminiTranscriber(
        client=FakeClient(interaction=FakeInteraction(output_text="t1", usage=None))
    )
    t1.transcribe(b"RIFFfake-wav")
    assert t1.last_debug() is not None
    assert t1.last_debug().usage is None

    # Case 2: usage is empty dict
    t2 = GeminiTranscriber(
        client=FakeClient(interaction=FakeInteraction(output_text="t2", usage={}))
    )
    t2.transcribe(b"RIFFfake-wav")
    assert t2.last_debug() is not None
    assert t2.last_debug().usage is None

    # Case 3: usage with invalid types / booleans
    t3 = GeminiTranscriber(
        client=FakeClient(
            interaction=FakeInteraction(
                output_text="t3",
                usage={"total_input_tokens": "not-an-int", "total_cached_tokens": True},
            )
        )
    )
    t3.transcribe(b"RIFFfake-wav")
    assert t3.last_debug() is not None
    assert t3.last_debug().usage is None

    # Case 4: interaction without usage attribute
    class BareInteraction:
        output_text = "t4"

    t4 = GeminiTranscriber(client=FakeClient(interaction=BareInteraction()))
    t4.transcribe(b"RIFFfake-wav")
    assert t4.last_debug() is not None
    assert t4.last_debug().usage is None


def test_transcriber_rejects_floats_negatives_and_decimal_strings_in_usage() -> None:
    # All invalid / float / negative / decimal strings -> usage is None
    all_invalid = {
        "total_input_tokens": -5,
        "total_output_tokens": 1.9,
        "total_thought_tokens": "1.9",
        "total_cached_tokens": "1.0",
        "total_tool_use_tokens": -1,
        "total_tokens": 2.5,
    }
    t_invalid = GeminiTranscriber(
        client=FakeClient(interaction=FakeInteraction(output_text="inv", usage=all_invalid))
    )
    t_invalid.transcribe(b"RIFFfake-wav")
    assert t_invalid.last_debug() is not None
    assert t_invalid.last_debug().usage is None

    # Mixed valid integer and invalid fields -> only valid non-negative ints preserved
    mixed_usage = {
        "total_input_tokens": 10,
        "total_output_tokens": 1.9,
        "total_thought_tokens": -5,
        "total_cached_tokens": "1.9",
        "total_tool_use_tokens": "1.0",
        "total_tokens": 10,
    }
    t_mixed = GeminiTranscriber(
        client=FakeClient(interaction=FakeInteraction(output_text="mix", usage=mixed_usage))
    )
    t_mixed.transcribe(b"RIFFfake-wav")
    debug = t_mixed.last_debug()
    assert debug is not None
    assert debug.usage is not None
    assert debug.usage.input_tokens == 10
    assert debug.usage.output_tokens is None
    assert debug.usage.thought_tokens is None
    assert debug.usage.cached_tokens is None
    assert debug.usage.tool_use_tokens is None
    assert debug.usage.total_tokens == 10


def test_transcriber_rejects_alias_only_usage_dict_and_object() -> None:
    # Alias-only dict (unofficial names)
    alias_dict = {
        "input_tokens": 10,
        "output_tokens": 4,
        "thought_tokens": 2,
        "cached_tokens": 1,
        "tool_use_tokens": 3,
        "total_tokens_count": 20,
    }
    t_dict = GeminiTranscriber(
        client=FakeClient(interaction=FakeInteraction(output_text="alias", usage=alias_dict))
    )
    t_dict.transcribe(b"RIFFfake-wav")
    assert t_dict.last_debug() is not None
    assert t_dict.last_debug().usage is None

    # Alias-only object
    class AliasUsageObject:
        input_tokens = 10
        output_tokens = 4
        thought_tokens = 2
        cached_tokens = 1
        tool_use_tokens = 3

    t_obj = GeminiTranscriber(
        client=FakeClient(interaction=FakeInteraction(output_text="alias-obj", usage=AliasUsageObject()))
    )
    t_obj.transcribe(b"RIFFfake-wav")
    assert t_obj.last_debug() is not None
    assert t_obj.last_debug().usage is None


def test_transcriber_retains_usage_none_on_local_and_api_errors() -> None:
    # Local error: empty audio
    t_empty = GeminiTranscriber(client=FakeClient())
    with pytest.raises(TranscriptionError, match="vazio"):
        t_empty.transcribe(b"")
    assert t_empty.last_debug() is not None
    assert t_empty.last_debug().usage is None

    # Local error: oversized audio
    t_over = GeminiTranscriber(client=FakeClient())
    with pytest.raises(TranscriptionError, match="longa demais"):
        t_over.transcribe(b"x" * (INLINE_LIMIT_BYTES + 1))
    assert t_over.last_debug() is not None
    assert t_over.last_debug().usage is None

    # API error: exception raised during interactions.create
    client_err = FakeClient()
    client_err.interactions.create = lambda **kwargs: (_ for _ in ()).throw(
        RuntimeError("500 Internal Server Error")
    )
    t_api = GeminiTranscriber(client=client_err)
    with pytest.raises(TranscriptionError, match="indisponível"):
        t_api.transcribe(b"RIFFfake-wav")
    assert t_api.last_debug() is not None
    assert t_api.last_debug().usage is None

def test_transcriber_records_usage_on_empty_response_error() -> None:
    raw_usage = {
        "total_input_tokens": 15,
        "total_output_tokens": 0,
        "total_thought_tokens": 0,
        "total_cached_tokens": 0,
        "total_tool_use_tokens": 0,
        "total_tokens": 15,
    }
    interaction = FakeInteraction(output_text="   ", usage=raw_usage)
    transcriber = GeminiTranscriber(client=FakeClient(interaction=interaction))

    with pytest.raises(TranscriptionError, match="não retornou texto"):
        transcriber.transcribe(b"RIFFfake-wav")

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.response_text == ""
    assert "não retornou texto" in (debug.error or "")
    assert debug.usage is not None
    assert debug.usage.input_tokens == 15
    assert debug.usage.output_tokens == 0
    assert debug.usage.thought_tokens == 0
    assert debug.usage.cached_tokens == 0
    assert debug.usage.tool_use_tokens == 0
    assert debug.usage.total_tokens == 15


def test_worker_emits_debug_with_usage_on_success_and_failure() -> None:
    QApplication.instance() or QApplication([])

    # Success case
    usage_payload = {"total_input_tokens": 20, "total_output_tokens": 8, "total_tokens": 28}
    interaction_ok = FakeInteraction(output_text="Sucesso", usage=usage_payload)
    transcriber_ok = GeminiTranscriber(client=FakeClient(interaction=interaction_ok))
    worker_ok = TranscriptionWorker(transcriber_ok, b"RIFFfake-wav")
    ok_received: list[tuple[str, Any]] = []
    worker_ok.finished.connect(lambda text, debug: ok_received.append((text, debug)))
    worker_ok.run()

    assert len(ok_received) == 1
    text, debug_ok = ok_received[0]
    assert text == "Sucesso"
    assert debug_ok.usage is not None
    assert debug_ok.usage.input_tokens == 20
    assert debug_ok.usage.output_tokens == 8
    assert debug_ok.usage.total_tokens == 28

    # Empty response failure case
    interaction_fail = FakeInteraction(output_text="", usage=usage_payload)
    transcriber_fail = GeminiTranscriber(client=FakeClient(interaction=interaction_fail))
    worker_fail = TranscriptionWorker(transcriber_fail, b"RIFFfake-wav")
    fail_received: list[tuple[str, Any]] = []
    worker_fail.failed.connect(lambda err, debug: fail_received.append((err, debug)))
    worker_fail.run()

    assert len(fail_received) == 1
    err, debug_fail = fail_received[0]
    assert "não retornou texto" in err
    assert debug_fail.usage is not None
    assert debug_fail.usage.input_tokens == 20
    assert debug_fail.usage.output_tokens == 8
    assert debug_fail.usage.total_tokens == 28


def test_worker_unexpected_exception_emits_generic_error_without_sensitive_details() -> None:
    QApplication.instance() or QApplication([])
    secret = "synthetic-secret-token-do-not-leak"

    class ExplodingTranscriber:
        def __init__(self) -> None:
            self._debug = TranscriptionDebug(
                model="synthetic-model",
                prompt=PROMPT,
                audio_bytes=10,
                audio_mime_type="audio/wav",
                audio_base64_length=16,
                audio_base64_preview="preview",
                response_text="",
                error=None,
                usage=None,
            )

        def transcribe(self, wav_bytes: bytes) -> str:
            raise RuntimeError(f"internal crash with token {secret}")

        def last_debug(self) -> TranscriptionDebug | None:
            return self._debug

    worker = TranscriptionWorker(ExplodingTranscriber(), b"RIFFfake-wav")  # type: ignore[arg-type]
    received: list[tuple[str, Any]] = []
    worker.failed.connect(lambda err, debug: received.append((err, debug)))

    worker.run()

    assert len(received) == 1
    err, debug = received[0]
    assert err == "Falha inesperada na transcrição."
    assert secret not in err
    assert debug is not None
    assert secret not in (debug.error or "")
    assert secret not in (debug.response_text or "")

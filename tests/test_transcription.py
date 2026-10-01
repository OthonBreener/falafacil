from __future__ import annotations

import asyncio
import base64
from dataclasses import FrozenInstanceError
import types
from typing import Any

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

import falafacil.transcription
from falafacil.audio import MAX_CAPTURE_WAV_BYTES, PcmChunkQueue
from falafacil.config import DEFAULT_MODEL
from falafacil.transcription import (
    INLINE_LIMIT_BYTES,
    LIVE_FINAL_TIMEOUT_MS,
    LIVE_LANGUAGE,
    LIVE_MIME_TYPE,
    LIVE_MODEL,
    LIVE_MODE,
    PROMPT,
    REQUEST_TIMEOUT_MS,
    GeminiTranscriber,
    LiveTranscriptionDebug,
    LiveTranscriptionResult,
    LiveTranscriptionWorker,
    TokenUsage,
    TranscriptionDebug,
    TranscriptionError,
    TranscriptionWorker,
    _extract_live_usage,
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
    assert set(call.keys()) == {"model", "input", "store"}
    assert call["store"] is False
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



def test_transcription_prompt_contract() -> None:
    assert isinstance(PROMPT, str)
    assert len(PROMPT.strip()) > 0
    assert "português do Brasil" in PROMPT
    assert "fidelidade ao sentido original" in PROMPT
    assert "correções sutis de fala" in PROMPT
    assert "hesitações" in PROMPT
    assert "cacoetes" in PROMPT
    assert "concordância" in PROMPT
    assert "nomes próprios" in PROMPT
    assert "termos técnicos" in PROMPT
    assert "não invente conteúdo" in PROMPT
    assert "texto simples pronto para copiar" in PROMPT


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
    assert INLINE_LIMIT_BYTES == MAX_CAPTURE_WAV_BYTES
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


class FakeLiveSession:
    def __init__(
        self,
        turns_messages: list[list[Any]] | None = None,
        *,
        raise_on_receive: Exception | None = None,
        hang_after_messages: bool = False,
        hang_on_connect: bool = False,
        hang_on_send: bool = False,
        turn_delays: dict[int, float] | None = None,
    ) -> None:
        self.sent_realtime_inputs: list[dict[str, Any]] = []
        self.turns_messages = list(turns_messages or [])
        self.raise_on_receive = raise_on_receive
        self.hang_after_messages = hang_after_messages
        self.hang_on_connect = hang_on_connect
        self.hang_on_send = hang_on_send
        self.turn_delays = turn_delays or {}
        self.closed = False
        self.current_turn = 0
        self.stream_end_received = asyncio.Event()

    async def send_realtime_input(
        self,
        *,
        audio: Any = None,
        audio_stream_end: bool | None = None,
        **kwargs: Any,
    ) -> None:
        self.sent_realtime_inputs.append(
            {
                "audio": audio,
                "audio_stream_end": audio_stream_end,
                **kwargs,
            }
        )
        if audio_stream_end:
            self.stream_end_received.set()
        if self.hang_on_send:
            await asyncio.sleep(3600)

    async def receive(self):
        if self.raise_on_receive is not None:
            raise self.raise_on_receive
        if self.current_turn < len(self.turns_messages):
            turn_idx = self.current_turn
            if (
                turn_idx == len(self.turns_messages) - 1
                and not self.stream_end_received.is_set()
            ):
                return
            messages = self.turns_messages[turn_idx]
            self.current_turn += 1
            for msg in messages:
                yield msg
            delay = self.turn_delays.get(turn_idx, 0.0)
            if delay > 0.0:
                await asyncio.sleep(delay)
        elif self.hang_after_messages:
            await asyncio.sleep(3600)

    async def __aenter__(self):
        if self.hang_on_connect:
            await asyncio.sleep(3600)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.closed = True


class FakeLiveClient:
    def __init__(self, session: FakeLiveSession) -> None:
        self.session = session
        self.connect_calls: list[dict[str, Any]] = []

        class FakeLive:
            def __init__(self, outer: FakeLiveClient) -> None:
                self._outer = outer

            def connect(self, *, model: str, config: Any) -> FakeLiveSession:
                self._outer.connect_calls.append({"model": model, "config": config})
                return self._outer.session

        class FakeAio:
            def __init__(self, outer: FakeLiveClient) -> None:
                self.live = FakeLive(outer)

        self.aio = FakeAio(self)


def _make_server_message(
    *,
    interim_text: str | None = None,
    final_text: str | None = None,
    finished: bool | None = None,
    turn_complete: bool = False,
    generation_complete: bool = False,
    usage: dict[str, Any] | None = None,
) -> types.SimpleNamespace:
    server_content = None
    if interim_text is not None or final_text is not None or turn_complete or generation_complete:
        interim_obj = types.SimpleNamespace(text=interim_text) if interim_text is not None else None
        input_obj = (
            types.SimpleNamespace(text=final_text, finished=finished)
            if final_text is not None
            else None
        )
        server_content = types.SimpleNamespace(
            interim_input_transcription=interim_obj,
            input_transcription=input_obj,
            turn_complete=turn_complete,
            generation_complete=generation_complete,
        )
    usage_meta = None
    if usage is not None:
        usage_meta = types.SimpleNamespace(**usage)
    return types.SimpleNamespace(
        server_content=server_content,
        usage_metadata=usage_meta,
    )


def test_live_worker_configures_smart_pt_br_and_model() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Olá mundo.", turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    worker = LiveTranscriptionWorker("test-key", queue, client=client, model="custom-live-model")
    worker.request_stop()
    worker.run()

    assert len(client.connect_calls) == 1
    call = client.connect_calls[0]
    assert call["model"] == "custom-live-model"
    config = call["config"]
    assert config.response_modalities == ["TEXT"]
    assert config.input_audio_transcription.language_codes == ["pt-BR"]
    assert config.input_audio_transcription.mode == "SMART"


def test_live_worker_streams_pcm_blobs_and_sends_audio_stream_end() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Segmento final.", turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    chunk1 = (np.ones(400, dtype=np.int16) * 1000).tobytes()
    chunk2 = (np.ones(400, dtype=np.int16) * 1200).tobytes()
    queue.enqueue(chunk1)
    queue.enqueue(chunk2)
    queue.finish()

    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.request_stop()
    worker.run()

    inputs = session.sent_realtime_inputs
    assert len(inputs) == 3
    assert inputs[0]["audio"].data == chunk1
    assert inputs[0]["audio"].mime_type == LIVE_MIME_TYPE
    assert inputs[1]["audio"].data == chunk2
    assert inputs[1]["audio"].mime_type == LIVE_MIME_TYPE
    assert inputs[2]["audio_stream_end"] is True


def test_live_worker_emits_interim_and_deduplicates_final_segments() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(interim_text="eu"),
            _make_server_message(interim_text="eu quero"),
            _make_server_message(final_text="Eu quero"),
            _make_server_message(final_text="Eu quero"),  # duplicate should be ignored
            _make_server_message(interim_text="falar"),
            _make_server_message(final_text="falar em português."),
            _make_server_message(
                turn_complete=True,
                usage={
                    "prompt_token_count": 50,
                    "response_token_count": 12,
                    "thoughts_token_count": None,
                    "cached_content_token_count": 10,
                    "tool_use_prompt_token_count": None,
                    "total_token_count": 62,
                },
            ),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    interim_events: list[str] = []
    final_events: list[str] = []
    finished_results: list[LiveTranscriptionResult] = []

    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.interim.connect(interim_events.append)
    worker.final.connect(final_events.append)
    worker.finished.connect(finished_results.append)

    worker.request_stop()
    worker.run()

    assert interim_events == ["eu", "eu quero", "falar"]
    assert final_events == ["Eu quero", "falar em português."]
    assert len(finished_results) == 1
    res = finished_results[0]
    assert res.text == "Eu quero falar em português."
    assert res.debug.model == LIVE_MODEL
    assert res.debug.language_code == LIVE_LANGUAGE
    assert res.debug.mode == LIVE_MODE
    assert res.debug.audio_bytes == 1600
    assert res.debug.usage == TokenUsage(
        input_tokens=50,
        output_tokens=12,
        thought_tokens=None,
        cached_tokens=10,
        tool_use_tokens=None,
        total_tokens=62,
    )


def test_live_worker_respects_finished_flag_and_ignores_unconfirmed_progressions() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Eu", finished=False),
            _make_server_message(final_text="Eu quero", finished=False),
            _make_server_message(final_text="Eu quero", finished=True),
            _make_server_message(final_text="corrigir esta fala.", finished=None),
            _make_server_message(turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    final_events: list[str] = []
    finished_results: list[LiveTranscriptionResult] = []

    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.final.connect(final_events.append)
    worker.finished.connect(finished_results.append)
    worker.request_stop()
    worker.run()

    assert final_events == ["Eu quero", "corrigir esta fala."]
    assert len(finished_results) == 1
    assert finished_results[0].text == "Eu quero corrigir esta fala."


def test_live_worker_loops_after_turn_complete_across_multiple_turns() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Primeira parte.", turn_complete=True),
        ],
        [
            _make_server_message(final_text="Segunda parte.", turn_complete=True),
        ],
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())

    finished_results: list[LiveTranscriptionResult] = []
    final_events: list[str] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)

    def on_final(text: str) -> None:
        final_events.append(text)
        if len(final_events) == 1:
            queue.finish()
            worker.request_stop()

    worker.final.connect(on_final)
    worker.finished.connect(finished_results.append)
    worker.run()

    assert len(finished_results) == 1
    assert finished_results[0].text == "Primeira parte. Segunda parte."
    assert final_events == ["Primeira parte.", "Segunda parte."]


def test_live_worker_preserves_final_received_in_subsequent_receive_after_prior_final() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession(
        [
            [
                _make_server_message(
                    final_text="Primeira parte confirmada.",
                    finished=True,
                    turn_complete=True,
                ),
            ],
            [
                _make_server_message(
                    final_text="Segunda parte confirmada.",
                    finished=True,
                    turn_complete=True,
                ),
            ],
        ],
        turn_delays={0: 0.05},
    )
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())

    interim_events: list[str] = []
    final_events: list[str] = []
    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker("test-key", queue, client=client)

    def on_final(text: str) -> None:
        final_events.append(text)
        if len(final_events) == 1:
            queue.finish()
            worker.request_stop()

    worker.interim.connect(interim_events.append)
    worker.final.connect(on_final)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.run()

    assert len(failed_events) == 0
    assert len(finished_results) == 1
    assert (
        finished_results[0].text
        == "Primeira parte confirmada. Segunda parte confirmada."
    )
    assert final_events == [
        "Primeira parte confirmada.",
        "Segunda parte confirmada.",
    ]
    assert session.closed is True


def test_live_worker_prior_final_followed_by_empty_or_interim_or_usage_post_end_cycle_times_out(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 100)
    session = FakeLiveSession(
        [
            [
                _make_server_message(
                    final_text="Fala anterior ao stop.",
                    finished=True,
                ),
            ],
            [
                _make_server_message(interim_text="fala pós-stop não confirmada"),
                _make_server_message(
                    usage={
                        "prompt_token_count": 40,
                        "response_token_count": 15,
                        "total_token_count": 55,
                    }
                ),
            ],
        ],
        turn_delays={0: 0.01},
        hang_after_messages=True,
    )
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())

    final_events: list[str] = []
    interim_events: list[str] = []
    failed_events: list[tuple[str, Any]] = []
    finished_results: list[LiveTranscriptionResult] = []

    worker = LiveTranscriptionWorker("test-key", queue, client=client)

    def on_final(text: str) -> None:
        final_events.append(text)
        if len(final_events) == 1:
            queue.finish()
            worker.request_stop()

    worker.interim.connect(interim_events.append)
    worker.final.connect(on_final)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.run()

    assert len(finished_results) == 0
    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert dbg.response_text == "Fala anterior ao stop."
    assert dbg.error == msg
    assert dbg.usage == TokenUsage(
        input_tokens=40,
        output_tokens=15,
        thought_tokens=None,
        cached_tokens=None,
        tool_use_tokens=None,
        total_tokens=55,
    )
    assert session.closed is True


def test_live_worker_turn_complete_post_end_followed_by_final_in_subsequent_receive_succeeds() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession(
        [
            [
                _make_server_message(
                    final_text="Segmento acumulado pré-stop.",
                    finished=True,
                ),
            ],
            [
                # Post-end turn_complete arrives first without final text
                _make_server_message(turn_complete=True),
            ],
            [
                # Final text arrives in subsequent receive cycle
                _make_server_message(
                    final_text="Segmento final pós-turn-complete.",
                    finished=True,
                ),
            ],
        ],
        turn_delays={0: 0.01, 1: 0.01},
    )
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    final_events: list[str] = []
    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker("test-key", queue, client=client)

    def on_final(text: str) -> None:
        final_events.append(text)
        if len(final_events) == 1:
            queue.finish()
            worker.request_stop()

    worker.final.connect(on_final)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.run()

    assert len(failed_events) == 0
    assert len(finished_results) == 1
    assert (
        finished_results[0].text
        == "Segmento acumulado pré-stop. Segmento final pós-turn-complete."
    )
    assert final_events == [
        "Segmento acumulado pré-stop.",
        "Segmento final pós-turn-complete.",
    ]
    assert session.closed is True

def test_live_worker_empty_transcription_times_out_and_emits_failed(monkeypatch) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 40)
    session = FakeLiveSession([], hang_after_messages=True)
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    failed_events: list[tuple[str, Any]] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.request_stop()
    worker.run()

    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert dbg.audio_bytes == 1600
    assert dbg.response_text == ""
    assert dbg.error == msg


def test_live_worker_empty_transcription_with_turn_complete_times_out_and_emits_sanitized_failure(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 40)
    session = FakeLiveSession([
        [
            _make_server_message(turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    failed_events: list[tuple[str, Any]] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.request_stop()
    worker.run()

    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert dbg.audio_bytes == 1600
    assert dbg.response_text == ""
    assert dbg.error == msg

def test_live_worker_buffer_overflow_emits_sanitized_failure() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Texto parcial"),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue(maxsize=1)
    queue.enqueue(b"chunk1")
    queue.enqueue(b"chunk2_overflow")  # Triggers overflow
    assert queue.overflowed

    failed_events: list[tuple[str, Any]] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.request_stop()
    worker.run()

    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "buffer de áudio ao vivo estourou" in msg


def test_live_worker_api_error_sanitizes_secret_and_classifies_error() -> None:
    QApplication.instance() or QApplication([])
    secret = "secret-token-live-abc12345"
    session = FakeLiveSession(
        raise_on_receive=RuntimeError(f"HTTP 401 Unauthorized with key {secret} at https://internal.google.com/endpoint")
    )
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    failed_events: list[tuple[str, Any]] = []
    worker = LiveTranscriptionWorker(secret, queue, client=client)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.request_stop()
    worker.run()

    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert msg == "Chave Gemini inválida ou ausente. Verifique GEMINI_API_KEY."
    assert secret not in msg
    assert "internal.google.com" not in msg
    assert dbg is not None
    assert secret not in (dbg.error or "")
    assert "internal.google.com" not in (dbg.error or "")


def test_live_usage_extraction_converts_all_live_fields() -> None:
    raw_live_usage = {
        "prompt_token_count": 100,
        "response_token_count": 25,
        "thoughts_token_count": 5,
        "cached_content_token_count": 40,
        "tool_use_prompt_token_count": 2,
        "total_token_count": 172,
    }
    usage = _extract_live_usage(raw_live_usage)
    assert usage == TokenUsage(
        input_tokens=100,
        output_tokens=25,
        thought_tokens=5,
        cached_tokens=40,
        tool_use_tokens=2,
        total_tokens=172,
    )

    # Handles all None
    assert _extract_live_usage(None) is None
    assert _extract_live_usage({}) is None


def test_live_worker_timeout_with_pending_receiver_emits_failed_and_preserves_partial_debug(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 50)

    session = FakeLiveSession(
        [
            [
                _make_server_message(interim_text="Segmento parcial não confirmado"),
                _make_server_message(
                    usage={
                        "prompt_token_count": 30,
                        "response_token_count": 10,
                        "total_token_count": 40,
                    }
                ),
            ]
        ],
        hang_after_messages=True,
    )
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    interim_events: list[str] = []
    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.interim.connect(interim_events.append)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    worker.request_stop()
    worker.run()

    assert interim_events == ["Segmento parcial não confirmado"]
    assert len(finished_results) == 0
    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert dbg.response_text == ""
    assert dbg.audio_bytes == 1600
    assert dbg.usage == TokenUsage(
        input_tokens=30,
        output_tokens=10,
        thought_tokens=None,
        cached_tokens=None,
        tool_use_tokens=None,
        total_tokens=40,
    )
    assert dbg.error == msg
    assert session.closed is True


def test_live_worker_pending_connect_times_out_and_emits_sanitized_failure(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 40)

    secret = "secret-token-live-connect-999"
    session = FakeLiveSession(hang_on_connect=True)
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker(secret, queue, client=client)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    worker.request_stop()
    worker.run()

    assert len(finished_results) == 0
    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert secret not in msg
    assert dbg is not None
    assert dbg.audio_bytes == 0
    assert secret not in (dbg.error or "")


def test_live_worker_pending_connect_force_cancel_terminates_cleanly_without_signals() -> None:
    import threading
    import time
    QApplication.instance() or QApplication([])

    session = FakeLiveSession(hang_on_connect=True)
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker("key", queue, client=client)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    thread = threading.Thread(target=worker.run)
    thread.start()

    time.sleep(0.05)
    worker.force_cancel()
    thread.join(timeout=1.0)

    assert thread.is_alive() is False
    assert len(finished_results) == 0
    assert len(failed_events) == 0


def test_live_worker_pending_send_times_out_and_emits_sanitized_failure(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 40)

    secret = "secret-token-live-send-888"
    session = FakeLiveSession(hang_on_send=True)
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker(secret, queue, client=client)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    worker.request_stop()
    worker.run()

    assert len(finished_results) == 0
    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert secret not in msg
    assert dbg is not None
    assert secret not in (dbg.error or "")
    assert session.closed is True


def test_live_worker_pending_send_force_cancel_terminates_cleanly_without_signals() -> None:
    import threading
    import time
    QApplication.instance() or QApplication([])

    session = FakeLiveSession(hang_on_send=True)
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())
    queue.finish()

    finished_results: list[LiveTranscriptionResult] = []
    failed_events: list[tuple[str, Any]] = []

    worker = LiveTranscriptionWorker("key", queue, client=client)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    thread = threading.Thread(target=worker.run)
    thread.start()

    time.sleep(0.05)
    worker.force_cancel()
    thread.join(timeout=1.0)

    assert thread.is_alive() is False
    assert len(finished_results) == 0
    assert len(failed_events) == 0
    assert session.closed is True


def test_live_worker_below_rms_threshold_never_sends_pcm_blobs_and_sends_audio_stream_end(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 40)
    session = FakeLiveSession([
        [
            _make_server_message(turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    # Low amplitude chunks (sample value 1, RMS ~ 0.00003 < MIN_RMS_LEVEL 0.005)
    low_chunk1 = b"\x01\x00" * 800
    low_chunk2 = b"\x00\x00" * 800
    queue.enqueue(low_chunk1)
    queue.enqueue(low_chunk2)
    queue.finish()

    failed_events: list[tuple[str, Any]] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))
    worker.request_stop()
    worker.run()

    # Assert no audio blobs were transmitted to the session
    assert len(session.sent_realtime_inputs) == 1
    assert session.sent_realtime_inputs[0].get("audio") is None
    assert session.sent_realtime_inputs[0].get("audio_stream_end") is True
    assert session.closed is True

    # Assert worker emitted failed with audio_bytes == 0
    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert dbg.audio_bytes == 0

def test_live_worker_gating_accumulates_until_threshold_reached_then_flushes_all_in_order() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Áudio ativado com sucesso.", turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()

    # Chunk 1: silence (RMS = 0 < 0.005) -> should be buffered
    chunk1 = (np.zeros(400, dtype=np.int16)).tobytes()
    # Chunk 2: voice (RMS of chunk1+chunk2 will be ~ 0.021 > 0.005) -> opens gate and flushes chunk1 & chunk2
    chunk2 = (np.ones(400, dtype=np.int16) * 1000).tobytes()
    # Chunk 3: subsequent voice chunk -> sent directly
    chunk3 = (np.ones(400, dtype=np.int16) * 1500).tobytes()

    queue.enqueue(chunk1)
    queue.enqueue(chunk2)
    queue.enqueue(chunk3)
    queue.finish()

    finished_results: list[LiveTranscriptionResult] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.finished.connect(finished_results.append)
    worker.request_stop()
    worker.run()

    inputs = session.sent_realtime_inputs
    assert len(inputs) == 4
    assert inputs[0]["audio"].data == chunk1
    assert inputs[1]["audio"].data == chunk2
    assert inputs[2]["audio"].data == chunk3
    assert inputs[3]["audio_stream_end"] is True

    assert len(finished_results) == 1
    res = finished_results[0]
    assert res.text == "Áudio ativado com sucesso."
    assert res.debug.audio_bytes == len(chunk1) + len(chunk2) + len(chunk3)
    assert session.closed is True


def test_live_worker_receive_started_before_stream_end_confirms_on_single_cycle_after_stream_end() -> None:
    QApplication.instance() or QApplication([])

    class SingleCyclePreEndSession:
        def __init__(self) -> None:
            self.cycle = 0
            self.closed = False
            self.sent_realtime_inputs: list[dict[str, Any]] = []
            self.stream_end_sent = asyncio.Event()

        async def send_realtime_input(self, **kwargs) -> None:
            self.sent_realtime_inputs.append(kwargs)
            if kwargs.get("audio_stream_end"):
                self.stream_end_sent.set()

        async def receive(self):
            current = self.cycle
            self.cycle += 1
            if current == 0:
                # receive() was opened BEFORE stream-end was sent
                await self.stream_end_sent.wait()
                # Yield single final+turn_complete caused by audio_stream_end in this same receive call
                yield _make_server_message(
                    final_text="Texto confirmado no mesmo ciclo após stream end.",
                    finished=True,
                    turn_complete=True,
                )
            else:
                # If worker mistakenly requires a second cycle, hang
                await asyncio.sleep(3600)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

    session = SingleCyclePreEndSession()
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())

    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    finished_results: list[LiveTranscriptionResult] = []
    final_events: list[str] = []
    failed_events: list[tuple[str, Any]] = []
    worker.final.connect(final_events.append)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    import threading
    thread = threading.Thread(target=worker.run)
    thread.start()

    queue.finish()
    worker.request_stop()
    thread.join(timeout=3.0)

    assert thread.is_alive() is False
    QApplication.processEvents()
    assert len(failed_events) == 0
    assert len(finished_results) == 1
    assert (
        finished_results[0].text
        == "Texto confirmado no mesmo ciclo após stream end."
    )
    assert session.closed is True
    assert session.cycle == 1


def test_live_worker_final_before_stream_end_in_same_receive_call_does_not_confirm_without_post_end_event(
    monkeypatch,
) -> None:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(falafacil.transcription, "LIVE_FINAL_TIMEOUT_MS", 100)

    class FinalBeforeEndInSameCallSession:
        def __init__(self) -> None:
            self.cycle = 0
            self.closed = False
            self.sent_realtime_inputs: list[dict[str, Any]] = []
            self.stream_end_sent = asyncio.Event()

        async def send_realtime_input(self, **kwargs) -> None:
            self.sent_realtime_inputs.append(kwargs)
            if kwargs.get("audio_stream_end"):
                self.stream_end_sent.set()

        async def receive(self):
            current = self.cycle
            self.cycle += 1
            if current == 0:
                # 1. Yield final BEFORE stream_end is sent
                yield _make_server_message(
                    final_text="Texto pré-fim não pode confirmar encerramento.",
                    finished=True,
                )
                # 2. Wait until stream_end is sent
                await self.stream_end_sent.wait()
                # 3. Yield interim and usage only (no turn_complete, no new final)
                yield _make_server_message(interim_text="interim após fim")
                yield _make_server_message(
                    usage={
                        "prompt_token_count": 10,
                        "response_token_count": 5,
                        "total_token_count": 15,
                    }
                )
                # 4. Hang so receiver must time out if not confirmed
                await asyncio.sleep(3600)
            else:
                await asyncio.sleep(3600)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            self.closed = True

    session = FinalBeforeEndInSameCallSession()
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()
    queue.enqueue((np.ones(800, dtype=np.int16) * 1000).tobytes())

    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    finished_results: list[LiveTranscriptionResult] = []
    final_events: list[str] = []
    failed_events: list[tuple[str, Any]] = []

    def on_final(text: str) -> None:
        final_events.append(text)
        # When first final is received, stop the worker/queue so sender sends stream_end
        queue.finish()
        worker.request_stop()

    worker.final.connect(on_final)
    worker.finished.connect(finished_results.append)
    worker.failed.connect(lambda msg, dbg: failed_events.append((msg, dbg)))

    worker.run()

    assert len(finished_results) == 0
    assert len(failed_events) == 1
    msg, dbg = failed_events[0]
    assert "tempo limite" in msg
    assert dbg.response_text == "Texto pré-fim não pode confirmar encerramento."
    assert session.closed is True


def test_live_worker_many_silent_chunks_accumulate_incrementally_then_open_gate_and_flush_in_order() -> None:
    QApplication.instance() or QApplication([])
    session = FakeLiveSession([
        [
            _make_server_message(final_text="Fala detectada após silêncio longo.", turn_complete=True),
        ]
    ])
    client = FakeLiveClient(session)
    queue = PcmChunkQueue()

    # Enqueue 60 small low-amplitude chunks (each 160 bytes of zeros/near-zero)
    silent_chunks: list[bytes] = []
    for i in range(60):
        chunk = (np.ones(80, dtype=np.int16) * (i % 2)).tobytes()
        silent_chunks.append(chunk)
        queue.enqueue(chunk)

    # Loud voice chunk that pulls the accumulated RMS above MIN_RMS_LEVEL
    loud_chunk = (np.ones(1600, dtype=np.int16) * 4000).tobytes()
    queue.enqueue(loud_chunk)

    # Subsequent chunk sent after gate is already open
    post_gate_chunk = (np.ones(800, dtype=np.int16) * 2000).tobytes()
    queue.enqueue(post_gate_chunk)
    queue.finish()

    finished_results: list[LiveTranscriptionResult] = []
    worker = LiveTranscriptionWorker("test-key", queue, client=client)
    worker.finished.connect(finished_results.append)
    worker.request_stop()
    worker.run()

    inputs = session.sent_realtime_inputs
    # 60 silent chunks + 1 loud chunk + 1 post-gate chunk + 1 audio_stream_end
    assert len(inputs) == 63
    for i in range(60):
        assert inputs[i]["audio"].data == silent_chunks[i]
        assert inputs[i]["audio"].mime_type == LIVE_MIME_TYPE
    assert inputs[60]["audio"].data == loud_chunk
    assert inputs[61]["audio"].data == post_gate_chunk
    assert inputs[62]["audio_stream_end"] is True

    assert len(finished_results) == 1
    res = finished_results[0]
    assert res.text == "Fala detectada após silêncio longo."
    expected_bytes = sum(len(c) for c in silent_chunks) + len(loud_chunk) + len(post_gate_chunk)
    assert res.debug.audio_bytes == expected_bytes
    assert session.closed is True

from __future__ import annotations

from typing import Any
import pytest
from PySide6.QtWidgets import QApplication

from falafacil.transcription import (
    PROOFREADING_PROMPT,
    GeminiTranscriber,
    ProofreadingWorker,
    TokenUsage,
    TranscriptionDebug,
    TranscriptionError,
)


class FakeInteraction:
    def __init__(
        self,
        output_text: str = "  Texto revisado com sucesso.  ",
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


def test_proofreading_prompt_contract() -> None:
    assert isinstance(PROOFREADING_PROMPT, str)
    assert len(PROOFREADING_PROMPT.strip()) > 0
    assert "revisor gramatical e ortográfico" in PROOFREADING_PROMPT
    assert "português do Brasil" in PROOFREADING_PROMPT
    assert "REGRAS INVIOLÁVEIS" in PROOFREADING_PROMPT
    assert "texto simples pronto para copiar" in PROOFREADING_PROMPT


@pytest.mark.parametrize("empty_text", ["", "   ", "\n\t  \n"])
def test_proofread_rejects_empty_text_without_calling_api(empty_text: str) -> None:
    client = FakeClient()
    transcriber = GeminiTranscriber(client=client)

    with pytest.raises(TranscriptionError, match="O texto para revisão está vazio."):
        transcriber.proofread(empty_text)

    assert len(client.interactions.calls) == 0
    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.prompt == PROOFREADING_PROMPT
    assert debug.audio_bytes == 0
    assert debug.audio_mime_type == ""
    assert debug.error == "O texto para revisão está vazio."
    assert debug.response_text == ""


def test_proofread_success_sends_prompt_and_returns_trimmed_text() -> None:
    interaction = FakeInteraction(
        output_text="  Texto corrigido e pontuado.  ",
        usage={
            "total_input_tokens": 40,
            "total_output_tokens": 15,
            "total_thought_tokens": 0,
            "total_cached_tokens": 5,
            "total_tool_use_tokens": 0,
            "total_tokens": 55,
        },
    )
    client = FakeClient(interaction=interaction)
    transcriber = GeminiTranscriber(client=client, model="gemini-3.5-flash-lite")

    input_text = "texto com erro de pontuacao e concordancia"
    result = transcriber.proofread(input_text)

    assert result == "Texto corrigido e pontuado."
    assert len(client.interactions.calls) == 1
    call = client.interactions.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    assert len(call["input"]) == 1
    assert call["input"][0]["type"] == "text"
    expected_prompt = f"{PROOFREADING_PROMPT}\n\nTexto:\n{input_text}"
    assert call["input"][0]["text"] == expected_prompt

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.model == "gemini-3.5-flash-lite"
    assert debug.prompt == PROOFREADING_PROMPT
    assert debug.audio_bytes == len(input_text.encode("utf-8"))
    assert debug.audio_mime_type == ""
    assert debug.audio_base64_length == 0
    assert debug.audio_base64_preview == ""
    assert debug.response_text == "Texto corrigido e pontuado."
    assert debug.error is None
    assert isinstance(debug.usage, TokenUsage)
    assert debug.usage.input_tokens == 40
    assert debug.usage.output_tokens == 15
    assert debug.usage.cached_tokens == 5
    assert debug.usage.total_tokens == 55


def test_proofread_records_exact_text_byte_count_including_utf8() -> None:
    interaction = FakeInteraction(output_text="Texto ok.")
    client = FakeClient(interaction=interaction)
    transcriber = GeminiTranscriber(client=client)

    text_with_accents = "Atenção: áéíóú çãõ, pontuação e acentuação!"
    expected_bytes = len(text_with_accents.encode("utf-8"))
    assert expected_bytes > len(text_with_accents)

    transcriber.proofread(text_with_accents)
    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.audio_bytes == expected_bytes
    assert debug.audio_mime_type == ""

def test_proofread_handles_dict_interaction_response() -> None:
    dict_interaction = {
        "output_text": "  Revisão em dicionário.  ",
        "usage": {"total_tokens": 25, "total_input_tokens": 15, "total_output_tokens": 10},
    }
    client = FakeClient(interaction=dict_interaction)
    transcriber = GeminiTranscriber(client=client)

    result = transcriber.proofread("frase para teste")
    assert result == "Revisão em dicionário."
    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.response_text == "Revisão em dicionário."
    assert debug.usage is not None
    assert debug.usage.total_tokens == 25


@pytest.mark.parametrize("empty_response", ["", "   ", None])
def test_proofread_raises_error_on_empty_api_response(empty_response: Any) -> None:
    interaction = FakeInteraction(
        output_text=empty_response,
        usage={"total_tokens": 30, "total_input_tokens": 20, "total_output_tokens": 10},
    )
    client = FakeClient(interaction=interaction)
    transcriber = GeminiTranscriber(client=client)

    with pytest.raises(TranscriptionError, match="O Gemini não retornou texto para a revisão."):
        transcriber.proofread("algum texto")

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.error == "O Gemini não retornou texto para a revisão."
    # Preserva o TokenUsage mesmo em resposta vazia
    assert debug.usage is not None
    assert debug.usage.total_tokens == 30


@pytest.mark.parametrize(
    ("raw_exception", "expected_message"),
    [
        (Exception("429 Resource has been exhausted (e.g. check quota)."), "Limite da API Gemini atingido. Tente novamente mais tarde."),
        (Exception("prepayment credit depleted"), "Créditos pré-pagos da API Gemini esgotados. Recarregue o projeto em https://ai.studio/projects."),
        (Exception("401 Unauthorized: Invalid API key"), "Chave Gemini inválida ou ausente. Verifique GEMINI_API_KEY."),
        (Exception("Request timed out after 120s"), "O Gemini não respondeu dentro do tempo limite. Tente novamente ou grave uma fala mais curta."),
        (Exception("503 Service Unavailable"), "O serviço Gemini está indisponível no momento. Tente novamente."),
    ],
)
def test_proofread_sanitizes_api_errors(raw_exception: Exception, expected_message: str) -> None:
    client = FakeClient()

    def fail(**kwargs: Any) -> Any:
        raise raw_exception

    client.interactions.create = fail
    transcriber = GeminiTranscriber(client=client)

    with pytest.raises(TranscriptionError, match=expected_message):
        transcriber.proofread("texto para revisar")

    debug = transcriber.last_debug()
    assert debug is not None
    assert debug.error == expected_message


def test_proofread_redacts_api_key_from_api_error() -> None:
    secret = "secret-key-12345-xyz"
    client = FakeClient()

    def fail(**kwargs: Any) -> Any:
        raise RuntimeError(f"Connection failed for key {secret} at host")

    client.interactions.create = fail
    transcriber = GeminiTranscriber(client=client, api_key=secret)

    with pytest.raises(TranscriptionError) as exc_info:
        transcriber.proofread("texto com chave")

    assert secret not in str(exc_info.value)
    debug = transcriber.last_debug()
    assert debug is not None
    assert secret not in (debug.error or "")


def test_proofreading_worker_emits_finished_signal_on_success() -> None:
    QApplication.instance() or QApplication([])
    interaction = FakeInteraction(
        output_text="Texto revisado pelo worker.",
        usage={"total_tokens": 12},
    )
    transcriber = GeminiTranscriber(client=FakeClient(interaction=interaction))
    worker = ProofreadingWorker(transcriber, "Texto original")

    assert worker.text == "Texto original"
    assert worker._text == "Texto original"

    finished_payloads = []
    worker.finished.connect(lambda text, debug: finished_payloads.append((text, debug)))

    worker.run()

    assert len(finished_payloads) == 1
    text, debug = finished_payloads[0]
    assert text == "Texto revisado pelo worker."
    assert isinstance(debug, TranscriptionDebug)
    assert debug.response_text == "Texto revisado pelo worker."
    assert debug.usage.total_tokens == 12


def test_proofreading_worker_emits_failed_signal_on_transcription_error() -> None:
    QApplication.instance() or QApplication([])
    transcriber = GeminiTranscriber(client=FakeClient())
    worker = ProofreadingWorker(transcriber, "   ")

    failed_payloads = []
    worker.failed.connect(lambda msg, debug: failed_payloads.append((msg, debug)))

    worker.run()

    assert len(failed_payloads) == 1
    msg, debug = failed_payloads[0]
    assert msg == "O texto para revisão está vazio."
    assert isinstance(debug, TranscriptionDebug)
    assert debug.error == "O texto para revisão está vazio."


def test_proofreading_worker_unexpected_exception_emits_sanitized_failure() -> None:
    QApplication.instance() or QApplication([])

    class BrokenTranscriber:
        def proofread(self, text: str) -> str:
            raise RuntimeError("segredo-vazado-12345 interno")

        def last_debug(self) -> TranscriptionDebug | None:
            return None

    worker = ProofreadingWorker(BrokenTranscriber(), "texto")  # type: ignore[arg-type]

    failed_payloads = []
    worker.failed.connect(lambda msg, debug: failed_payloads.append((msg, debug)))

    worker.run()

    assert len(failed_payloads) == 1
    msg, debug = failed_payloads[0]
    assert msg == "Falha inesperada na revisão do texto."
    assert "segredo" not in msg

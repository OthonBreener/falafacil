from __future__ import annotations

import base64

import pytest
from PySide6.QtWidgets import QApplication

from falafacil.config import DEFAULT_MODEL
from falafacil.transcription import (
    INLINE_LIMIT_BYTES,
    PROMPT,
    GeminiTranscriber,
    TranscriptionError,
    TranscriptionWorker,
)


class FakeInteraction:
    output_text = "  Olá, terminal!  "


class FakeInteractions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeInteraction()


class FakeClient:
    def __init__(self):
        self.interactions = FakeInteractions()


def test_transcriber_sends_inline_wav_and_returns_trimmed_text() -> None:
    client = FakeClient()
    transcriber = GeminiTranscriber(client=client)
    audio = b"RIFFfake-wav"

    result = transcriber.transcribe(audio)

    assert result == "Olá, terminal!"
    call = client.interactions.calls[0]
    assert call["model"] == DEFAULT_MODEL
    assert "português do Brasil" in call["input"][0]["text"]
    audio_part = call["input"][1]
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

    with pytest.raises(TranscriptionError):
        transcriber.transcribe(b"audio")

    debug = transcriber.last_debug()
    assert debug is not None
    assert "synthetic-client-token" not in (debug.error or "")


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
    assert calls == [{"api_key": "synthetic-client-token"}]


def test_transcriber_rejects_empty_and_oversized_audio() -> None:
    transcriber = GeminiTranscriber(client=FakeClient())

    with pytest.raises(TranscriptionError, match="vazio"):
        transcriber.transcribe(b"")
    with pytest.raises(TranscriptionError, match="longa demais"):
        transcriber.transcribe(b"x" * (INLINE_LIMIT_BYTES + 1))

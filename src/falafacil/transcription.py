from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from typing import Any

from google import genai
from PySide6.QtCore import QObject, Signal, Slot

from .config import DEFAULT_MODEL


INLINE_LIMIT_BYTES = 20 * 1024 * 1024
PROMPT = (
    "Transcreva somente o que foi falado neste áudio. "
    "O idioma é português do Brasil. Preserve nomes próprios e termos técnicos, "
    "corrija apenas a pontuação necessária e não invente conteúdo. "
    "Retorne apenas o texto simples pronto para copiar."
)


class TranscriptionError(RuntimeError):
    """Erro recuperável ao solicitar uma transcrição."""


@dataclass(frozen=True)
class TranscriptionDebug:
    model: str
    prompt: str
    audio_bytes: int
    audio_mime_type: str
    audio_base64_length: int
    audio_base64_preview: str
    response_text: str
    error: str | None


class GeminiTranscriber:
    def __init__(
        self,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        if client is not None:
            self.client = client
        elif api_key:
            self.client = genai.Client(api_key=api_key)
        else:
            self.client = genai.Client()
        self.model = model
        self._api_key = api_key or ""
        self._last_debug: TranscriptionDebug | None = None

    def last_debug(self) -> TranscriptionDebug | None:
        return self._last_debug

    def transcribe(self, wav_bytes: bytes) -> str:
        encoded_audio = _encode_preview(wav_bytes)
        self._last_debug = TranscriptionDebug(
            model=self.model,
            prompt=PROMPT,
            audio_bytes=len(wav_bytes),
            audio_mime_type="audio/wav",
            audio_base64_length=_base64_length(len(wav_bytes)),
            audio_base64_preview=encoded_audio[:128],
            response_text="",
            error=None,
        )
        if not wav_bytes:
            return self._raise_transcription_error("O áudio está vazio.")
        if len(wav_bytes) > INLINE_LIMIT_BYTES:
            return self._raise_transcription_error(
                "A fala ficou longa demais para o envio direto. Grave uma fala mais curta."
            )

        try:
            interaction = self.client.interactions.create(
                model=self.model,
                input=[
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "audio",
                        "data": encoded_audio,
                        "mime_type": "audio/wav",
                    },
                ],
            )
        except Exception as exc:
            raise self._transcription_error(
                _friendly_api_error(exc, secret=self._api_key)
            ) from exc

        text = str(getattr(interaction, "output_text", "")).strip()
        if not text:
            raise self._transcription_error(
                "O Gemini não retornou texto para este áudio."
            )
        self._last_debug = replace_debug(self._last_debug, response_text=text)
        return text

    def _raise_transcription_error(self, message: str) -> str:
        raise self._transcription_error(message)

    def _transcription_error(self, message: str) -> TranscriptionError:
        self._last_debug = replace_debug(self._last_debug, error=message)
        return TranscriptionError(message)


class TranscriptionWorker(QObject):
    finished = Signal(str, object)
    failed = Signal(str, object)

    def __init__(self, transcriber: GeminiTranscriber, wav_bytes: bytes) -> None:
        super().__init__()
        self._transcriber = transcriber
        self._wav_bytes = wav_bytes

    @Slot()
    def run(self) -> None:
        try:
            text = self._transcriber.transcribe(self._wav_bytes)
            self.finished.emit(text, _last_debug(self._transcriber))
        except TranscriptionError as exc:
            self.failed.emit(str(exc), _last_debug(self._transcriber))
        except Exception as exc:  # pragma: no cover - última barreira da UI
            self.failed.emit(
                f"Falha inesperada na transcrição: {exc}",
                _last_debug(self._transcriber),
            )


def _base64_length(byte_count: int) -> int:
    return ((byte_count + 2) // 3) * 4


def _encode_preview(wav_bytes: bytes) -> str:
    if len(wav_bytes) <= INLINE_LIMIT_BYTES:
        return base64.b64encode(wav_bytes).decode("utf-8")
    return base64.b64encode(wav_bytes[:96]).decode("utf-8")


def replace_debug(
    debug: TranscriptionDebug | None,
    **changes: Any,
) -> TranscriptionDebug | None:
    if debug is None:
        return None
    return replace(debug, **changes)


def _last_debug(transcriber: Any) -> TranscriptionDebug | None:
    getter = getattr(transcriber, "last_debug", None)
    return getter() if getter is not None else None

def _friendly_api_error(exc: Exception, *, secret: str = "") -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if secret:
        text = text.replace(secret, "[segredo omitido]")
    lowered = text.lower()
    if "401" in lowered or "authentication" in lowered or "api key" in lowered:
        return "Chave Gemini inválida ou ausente. Verifique GEMINI_API_KEY."
    if "404" in lowered or "model_not_found" in lowered:
        return "Modelo Gemini não encontrado. Confira GEMINI_MODEL."
    if "429" in lowered or "quota" in lowered or "rate_limit" in lowered:
        return "Limite da API Gemini atingido. Tente novamente mais tarde."
    if any(code in lowered for code in ("500", "503", "504", "unavailable", "deadline")):
        return "O serviço Gemini está indisponível no momento. Tente novamente."
    return f"Não foi possível transcrever: {text}"

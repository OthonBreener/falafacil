from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from typing import Any

from google import genai
from PySide6.QtCore import QObject, Signal, Slot

from .config import DEFAULT_MODEL


INLINE_LIMIT_BYTES = 20 * 1024 * 1024
REQUEST_TIMEOUT_MS = 120_000
PROMPT = (
    "Transcreva o que foi falado neste áudio em português do Brasil com fidelidade ao sentido original. "
    "Faça correções sutis de fala: elimine hesitações, gaguejos, repetições involuntárias, cacoetes (como 'né', 'tipo') "
    "e fragmentos desconexos, e ajuste pequenos deslizes gramaticais, de concordância (como 'do/da') ou palavras truncadas "
    "identificáveis pelo contexto imediato, sem alterar o sentido nem o vocabulário pretendido pelo locutor. "
    "Preserve nomes próprios e termos técnicos, corrija a pontuação e não invente conteúdo. "
    "Retorne apenas o texto simples pronto para copiar."
)

PROOFREADING_PROMPT = (
    "Você é um revisor gramatical e ortográfico especialista em português do Brasil.\n"
    "Revise o texto a seguir corrigindo rigorosamente:\n"
    "1. Erros ortográficos, acentuação e hífen (conforme o Acordo Ortográfico vigente).\n"
    "2. Concordância verbal e nominal, regência e crase.\n"
    "3. Pontuação (vírgulas, pontos finais, interrogações) para garantir fluidez e clareza natural.\n"
    "4. Homófonos contextuais comuns na transcrição de fala (ex: 'mas'/'mais', 'a'/'há', 'sessão'/'seção', 'mau'/'mal').\n"
    "REGRAS INVIOLÁVEIS:\n"
    "- Preserve fielmente o vocabulário, estilo, termos técnicos, nomes próprios, gírias e a intenção do locutor.\n"
    "- Não acrescente explicações, comentários, introduções ou notas.\n"
    "- Retorne exclusivamente o texto simples pronto para copiar."
)


class TranscriptionError(RuntimeError):
    """Erro recuperável ao solicitar uma transcrição."""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    thought_tokens: int | None = None
    cached_tokens: int | None = None
    tool_use_tokens: int | None = None
    total_tokens: int | None = None


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
    usage: TokenUsage | None = None

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
            self.client = genai.Client(
                api_key=api_key,
                http_options={"timeout": REQUEST_TIMEOUT_MS},
            )
        else:
            self.client = genai.Client(
                http_options={"timeout": REQUEST_TIMEOUT_MS},
            )
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
            usage=None,
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
                store=False,
            )
        except Exception as exc:
            raise self._transcription_error(
                _friendly_api_error(exc, secret=self._api_key)
            ) from exc

        raw_usage = getattr(interaction, "usage", None)
        if raw_usage is None and isinstance(interaction, dict):
            raw_usage = interaction.get("usage")
        usage = _extract_usage(raw_usage)
        if usage is not None:
            self._last_debug = replace_debug(self._last_debug, usage=usage)

        if isinstance(interaction, dict):
            output_text = interaction.get("output_text", "")
        else:
            output_text = getattr(interaction, "output_text", "")
        text = str(output_text or "").strip()
        if not text:
            raise self._transcription_error(
                "O Gemini não retornou texto para este áudio."
            )
        self._last_debug = replace_debug(self._last_debug, response_text=text)
        return text

    def proofread(self, text: str) -> str:
        is_empty = not isinstance(text, str) or not text.strip()
        text_bytes = 0 if is_empty else len(text.encode("utf-8"))
        self._last_debug = TranscriptionDebug(
            model=self.model,
            prompt=PROOFREADING_PROMPT,
            audio_bytes=text_bytes,
            audio_mime_type="",
            audio_base64_length=0,
            audio_base64_preview="",
            response_text="",
            error=None,
            usage=None,
        )
        if is_empty:
            return self._raise_transcription_error(
                "O texto para revisão está vazio."
            )

        prompt = f"{PROOFREADING_PROMPT}\n\nTexto:\n{text}"
        try:
            interaction = self.client.interactions.create(
                model=self.model,
                input=[{"type": "text", "text": prompt}],
                store=False,
            )
        except Exception as exc:
            raise self._transcription_error(
                _friendly_api_error(exc, secret=self._api_key)
            ) from exc

        raw_usage = getattr(interaction, "usage", None)
        if raw_usage is None and isinstance(interaction, dict):
            raw_usage = interaction.get("usage")
        usage = _extract_usage(raw_usage)
        if usage is not None:
            self._last_debug = replace_debug(self._last_debug, usage=usage)

        if isinstance(interaction, dict):
            output_text = interaction.get("output_text", "")
        else:
            output_text = getattr(interaction, "output_text", "")
        revised_text = str(output_text or "").strip()
        if not revised_text:
            raise self._transcription_error(
                "O Gemini não retornou texto para a revisão."
            )
        self._last_debug = replace_debug(self._last_debug, response_text=revised_text)
        return revised_text

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
        except Exception:
            self.failed.emit(
                "Falha inesperada na transcrição.",
                _last_debug(self._transcriber),
            )


class ProofreadingWorker(QObject):
    finished = Signal(str, object)
    failed = Signal(str, object)

    def __init__(self, transcriber: GeminiTranscriber, text: str) -> None:
        super().__init__()
        self._transcriber = transcriber
        self.text = text
        self._text = text

    @Slot()
    def run(self) -> None:
        try:
            revised_text = self._transcriber.proofread(self.text)
            self.finished.emit(revised_text, _last_debug(self._transcriber))
        except TranscriptionError as exc:
            self.failed.emit(str(exc), _last_debug(self._transcriber))
        except Exception:
            self.failed.emit(
                "Falha inesperada na revisão do texto.",
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


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isascii() and stripped.isdigit():
            return int(stripped)
    return None


def _get_field(raw: Any, key: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _extract_usage(raw_usage: Any) -> TokenUsage | None:
    if raw_usage is None:
        return None

    input_tokens = _to_int(_get_field(raw_usage, "total_input_tokens"))
    output_tokens = _to_int(_get_field(raw_usage, "total_output_tokens"))
    thought_tokens = _to_int(_get_field(raw_usage, "total_thought_tokens"))
    cached_tokens = _to_int(_get_field(raw_usage, "total_cached_tokens"))
    tool_use_tokens = _to_int(_get_field(raw_usage, "total_tool_use_tokens"))
    total_tokens = _to_int(_get_field(raw_usage, "total_tokens"))

    if all(
        v is None
        for v in (
            input_tokens,
            output_tokens,
            thought_tokens,
            cached_tokens,
            tool_use_tokens,
            total_tokens,
        )
    ):
        return None

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thought_tokens=thought_tokens,
        cached_tokens=cached_tokens,
        tool_use_tokens=tool_use_tokens,
        total_tokens=total_tokens,
    )

def _friendly_api_error(exc: Exception, *, secret: str = "") -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if secret:
        text = text.replace(secret, "[segredo omitido]")
    lowered = text.lower()
    if "401" in lowered or "authentication" in lowered or "api key" in lowered:
        return "Chave Gemini inválida ou ausente. Verifique GEMINI_API_KEY."
    if "404" in lowered or "model_not_found" in lowered:
        return "Modelo Gemini não encontrado. Confira GEMINI_MODEL."
    if _mentions_depleted_credits(lowered):
        return (
            "Créditos pré-pagos da API Gemini esgotados. "
            "Recarregue o projeto em https://ai.studio/projects."
        )
    if "429" in lowered or "quota" in lowered or "rate_limit" in lowered:
        return "Limite da API Gemini atingido. Tente novamente mais tarde."
    if any(code in lowered for code in ("500", "503", "504", "unavailable", "deadline")):
        return "O serviço Gemini está indisponível no momento. Tente novamente."
    if "timeout" in lowered or "timed out" in lowered:
        return (
            "O Gemini não respondeu dentro do tempo limite. "
            "Tente novamente ou grave uma fala mais curta."
        )
    return "Não foi possível transcrever o áudio."


def _mentions_depleted_credits(lowered: str) -> bool:
    if "prepayment" in lowered:
        return True
    return "credit" in lowered and any(
        marker in lowered for marker in ("deplet", "exhaust", "insufficient")
    )

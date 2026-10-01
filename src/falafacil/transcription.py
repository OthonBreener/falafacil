from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, replace
import math
import threading
from typing import Any

from google import genai
from google.genai import types
import numpy as np
from PySide6.QtCore import QObject, Signal, Slot

from .audio import (
    MAX_CAPTURE_WAV_BYTES,
    MIN_RMS_LEVEL,
    PcmChunkQueue,
)
from .config import DEFAULT_MODEL

INLINE_LIMIT_BYTES = MAX_CAPTURE_WAV_BYTES
REQUEST_TIMEOUT_MS = 120_000
LIVE_MODEL = "gemini-3.5-transcribe-live"
LIVE_MIME_TYPE = "audio/pcm;rate=16000"
LIVE_FINAL_TIMEOUT_MS = 10_000
LIVE_LANGUAGE = "pt-BR"
LIVE_MODE = "SMART"
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


@dataclass(frozen=True)
class LiveTranscriptionDebug:
    model: str
    language_code: str
    mode: str
    audio_bytes: int
    response_text: str
    error: str | None
    usage: TokenUsage | None = None


@dataclass(frozen=True)
class LiveTranscriptionResult:
    text: str
    debug: LiveTranscriptionDebug


class LiveTranscriptionWorker(QObject):
    interim = Signal(str)
    final = Signal(str)
    finished = Signal(object)
    failed = Signal(str, object)

    def __init__(
        self,
        api_key: str,
        audio_queue: PcmChunkQueue,
        *,
        client: Any | None = None,
        model: str = LIVE_MODEL,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._audio_queue = audio_queue
        self._client = client
        self._model = model
        self._stop_requested = False
        self._force_cancelled = False
        self._timed_out = False
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._deadline_handle: asyncio.TimerHandle | None = None

    def request_stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            loop = self._loop
            if (
                loop is not None
                and not loop.is_closed()
                and not self._force_cancelled
                and self._deadline_handle is None
            ):
                loop.call_soon_threadsafe(self._arm_deadline_in_loop)

    def cancel(self) -> None:
        self.force_cancel()

    def force_cancel(self) -> None:
        with self._lock:
            self._force_cancelled = True
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._cancel_all_tasks_in_loop)

    def _arm_deadline_in_loop(self) -> None:
        with self._lock:
            if self._force_cancelled or self._deadline_handle is not None:
                return
            if self._loop is not None and not self._loop.is_closed():
                self._deadline_handle = self._loop.call_later(
                    LIVE_FINAL_TIMEOUT_MS / 1000.0,
                    self._on_deadline_expired,
                )

    def _on_deadline_expired(self) -> None:
        with self._lock:
            self._timed_out = True
            self._deadline_handle = None
            if self._main_task is not None and not self._main_task.done():
                self._main_task.cancel()
            if self._sender_task is not None and not self._sender_task.done():
                self._sender_task.cancel()
            if self._receiver_task is not None and not self._receiver_task.done():
                self._receiver_task.cancel()

    def _cancel_all_tasks_in_loop(self) -> None:
        with self._lock:
            if self._deadline_handle is not None:
                self._deadline_handle.cancel()
                self._deadline_handle = None
            if self._main_task is not None and not self._main_task.done():
                self._main_task.cancel()
            if self._sender_task is not None and not self._sender_task.done():
                self._sender_task.cancel()
            if self._receiver_task is not None and not self._receiver_task.done():
                self._receiver_task.cancel()

    @Slot()
    def run(self) -> None:
        try:
            asyncio.run(self._async_run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._force_cancelled:
                err = _friendly_api_error(exc, secret=self._api_key)
                debug = LiveTranscriptionDebug(
                    model=self._model,
                    language_code=LIVE_LANGUAGE,
                    mode=LIVE_MODE,
                    audio_bytes=0,
                    response_text="",
                    error=err,
                    usage=None,
                )
                self.failed.emit(err, debug)

    async def _async_run(self) -> None:
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task(loop)
        with self._lock:
            self._loop = loop
            self._main_task = main_task
            if self._force_cancelled:
                if main_task is not None:
                    main_task.cancel()
            elif self._stop_requested and self._deadline_handle is None:
                self._arm_deadline_in_loop()

        total_bytes_sent = 0
        final_segments: list[str] = []
        last_usage: TokenUsage | None = None
        sent_stream_end = asyncio.Event()
        stop_receiver = asyncio.Event()
        receiver_done = asyncio.Event()

        client = self._client
        if client is None:
            client = genai.Client(
                api_key=self._api_key,
                http_options={"timeout": REQUEST_TIMEOUT_MS},
            )

        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=[LIVE_LANGUAGE],
                mode=LIVE_MODE,
            ),
        )

        gate_open = False
        pending_chunks: list[bytes] = []
        pending_sum_squares = 0.0
        pending_sample_count = 0

        async def process_chunk(chunk: bytes, session: Any) -> None:
            nonlocal total_bytes_sent, gate_open, pending_sum_squares, pending_sample_count
            if gate_open:
                total_bytes_sent += len(chunk)
                blob = types.Blob(data=chunk, mime_type=LIVE_MIME_TYPE)
                await session.send_realtime_input(audio=blob)
            else:
                pending_chunks.append(chunk)
                if chunk:
                    try:
                        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float64)
                        if samples.size > 0:
                            pending_sum_squares += float(np.sum(samples * samples))
                            pending_sample_count += samples.size
                    except (ValueError, TypeError):
                        pass
                if pending_sample_count > 0:
                    current_rms = (
                        math.sqrt(pending_sum_squares / pending_sample_count) / 32768.0
                    )
                    if current_rms >= MIN_RMS_LEVEL:
                        gate_open = True
                        for c in pending_chunks:
                            total_bytes_sent += len(c)
                            blob = types.Blob(data=c, mime_type=LIVE_MIME_TYPE)
                            await session.send_realtime_input(audio=blob)
                        pending_chunks.clear()
                        pending_sum_squares = 0.0
                        pending_sample_count = 0

        async def sender(session: Any) -> None:
            while not self._stop_requested:
                if self._audio_queue.overflowed:
                    raise TranscriptionError("O buffer de áudio ao vivo estourou.")
                chunk = await asyncio.to_thread(self._audio_queue.get, 0.05)
                if chunk is not None:
                    await process_chunk(chunk, session)

            while True:
                if self._audio_queue.overflowed:
                    raise TranscriptionError("O buffer de áudio ao vivo estourou.")
                chunk = await asyncio.to_thread(self._audio_queue.get, 0.01)
                if chunk is None:
                    break
                await process_chunk(chunk, session)

            await session.send_realtime_input(audio_stream_end=True)
            sent_stream_end.set()

        async def receiver(session: Any) -> None:
            nonlocal last_usage
            while not stop_receiver.is_set():
                try:
                    had_messages = False
                    post_stream_end_confirmed = False
                    is_post_stream_end_cycle = sent_stream_end.is_set()
                    stream_end_observed_during_cycle = False
                    async for message in session.receive():
                        had_messages = True
                        if sent_stream_end.is_set():
                            stream_end_observed_during_cycle = True
                        is_post_end_message = (
                            is_post_stream_end_cycle or stream_end_observed_during_cycle
                        )

                        server_content = _get_field(message, "server_content")
                        if server_content is not None:
                            interim_obj = _get_field(
                                server_content, "interim_input_transcription"
                            )
                            if interim_obj is not None:
                                interim_text = _get_field(interim_obj, "text")
                                if interim_text:
                                    self.interim.emit(str(interim_text))

                            input_obj = _get_field(
                                server_content, "input_transcription"
                            )
                            if input_obj is not None:
                                finished_val = _get_field(input_obj, "finished")
                                if finished_val is None or finished_val is True:
                                    final_text = _get_field(input_obj, "text")
                                    if final_text:
                                        text_str = str(final_text).strip()
                                        if text_str:
                                            if is_post_end_message:
                                                post_stream_end_confirmed = True
                                            if (
                                                not final_segments
                                                or final_segments[-1] != text_str
                                            ):
                                                final_segments.append(text_str)
                                                self.final.emit(text_str)

                        usage_meta = _get_field(message, "usage_metadata")
                        if usage_meta is not None:
                            extracted = _extract_live_usage(usage_meta)
                            if extracted is not None:
                                last_usage = extracted

                    if post_stream_end_confirmed:
                        receiver_done.set()
                        return
                    if not had_messages:
                        await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise

        sender_task: asyncio.Task[None] | None = None
        receiver_task: asyncio.Task[None] | None = None

        try:
            async with client.aio.live.connect(
                model=self._model, config=config
            ) as session:
                sender_task = asyncio.create_task(sender(session))
                receiver_task = asyncio.create_task(receiver(session))
                with self._lock:
                    self._sender_task = sender_task
                    self._receiver_task = receiver_task

                try:
                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        if task.exception() is not None:
                            raise task.exception()

                    if sender_task.done() and receiver_task in pending:
                        await receiver_task
                    elif receiver_task.done() and sender_task in pending:
                        sender_task.cancel()
                        try:
                            await sender_task
                        except (asyncio.CancelledError, Exception):
                            pass
                finally:
                    stop_receiver.set()
                    for task in (sender_task, receiver_task):
                        if task is not None and not task.done():
                            task.cancel()
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass

            if self._audio_queue.overflowed:
                raise TranscriptionError("O buffer de áudio ao vivo estourou.")

            final_text = " ".join(s for s in final_segments if s.strip()).strip()
            if not final_text:
                raise TranscriptionError(
                    "O Gemini não retornou texto para este áudio."
                )

            debug = LiveTranscriptionDebug(
                model=self._model,
                language_code=LIVE_LANGUAGE,
                mode=LIVE_MODE,
                audio_bytes=total_bytes_sent,
                response_text=final_text,
                error=None,
                usage=last_usage,
            )
            if not self._force_cancelled and not self._timed_out:
                self.finished.emit(
                    LiveTranscriptionResult(text=final_text, debug=debug)
                )

        except asyncio.CancelledError:
            if self._force_cancelled:
                return
            if self._timed_out:
                err_msg = "O Gemini não respondeu dentro do tempo limite."
            else:
                err_msg = "A transcrição ao vivo foi cancelada."
            final_text = " ".join(s for s in final_segments if s.strip()).strip()
            debug = LiveTranscriptionDebug(
                model=self._model,
                language_code=LIVE_LANGUAGE,
                mode=LIVE_MODE,
                audio_bytes=total_bytes_sent,
                response_text=final_text,
                error=err_msg,
                usage=last_usage,
            )
            self.failed.emit(err_msg, debug)
        except Exception as exc:
            if self._force_cancelled:
                return
            if self._timed_out or isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                err_msg = "O Gemini não respondeu dentro do tempo limite."
            else:
                err_msg = _friendly_api_error(exc, secret=self._api_key)
            final_text = " ".join(s for s in final_segments if s.strip()).strip()
            debug = LiveTranscriptionDebug(
                model=self._model,
                language_code=LIVE_LANGUAGE,
                mode=LIVE_MODE,
                audio_bytes=total_bytes_sent,
                response_text=final_text,
                error=err_msg,
                usage=last_usage,
            )
            self.failed.emit(err_msg, debug)
        finally:
            with self._lock:
                if self._deadline_handle is not None:
                    self._deadline_handle.cancel()
                    self._deadline_handle = None
                self._sender_task = None
                self._receiver_task = None
                self._main_task = None
                self._loop = None

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


def _extract_live_usage(raw_usage: Any) -> TokenUsage | None:
    if raw_usage is None:
        return None

    input_tokens = _to_int(
        _get_field(raw_usage, "prompt_token_count")
        if _get_field(raw_usage, "prompt_token_count") is not None
        else _get_field(raw_usage, "total_input_tokens")
    )
    output_tokens = _to_int(
        _get_field(raw_usage, "response_token_count")
        if _get_field(raw_usage, "response_token_count") is not None
        else _get_field(raw_usage, "total_output_tokens")
    )
    thought_tokens = _to_int(
        _get_field(raw_usage, "thoughts_token_count")
        if _get_field(raw_usage, "thoughts_token_count") is not None
        else _get_field(raw_usage, "total_thought_tokens")
    )
    cached_tokens = _to_int(
        _get_field(raw_usage, "cached_content_token_count")
        if _get_field(raw_usage, "cached_content_token_count") is not None
        else _get_field(raw_usage, "total_cached_tokens")
    )
    tool_use_tokens = _to_int(
        _get_field(raw_usage, "tool_use_prompt_token_count")
        if _get_field(raw_usage, "tool_use_prompt_token_count") is not None
        else _get_field(raw_usage, "total_tool_use_tokens")
    )
    total_tokens = _to_int(
        _get_field(raw_usage, "total_token_count")
        if _get_field(raw_usage, "total_token_count") is not None
        else _get_field(raw_usage, "total_tokens")
    )

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


def _extract_usage(raw_usage: Any) -> TokenUsage | None:
    return _extract_live_usage(raw_usage)
def _friendly_api_error(exc: Exception, *, secret: str = "") -> str:
    if isinstance(exc, TranscriptionError):
        return str(exc)
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

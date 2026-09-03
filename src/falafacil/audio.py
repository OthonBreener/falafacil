from __future__ import annotations

from dataclasses import dataclass
import io
import re
import threading
from typing import Any, Literal
import unicodedata
import wave

import numpy as np


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
MIN_RMS_LEVEL = 0.005
_SAMPLE_RATE_CANDIDATES = (SAMPLE_RATE, 48_000, 44_100, 32_000, 22_050, 8_000)


def _normalize_identifier(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[\W_]+", " ", without_accents.casefold())
    return " ".join(cleaned.split())

@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    is_default: bool
    host_api: str = ""
    kind: Literal["headset", "internal", "other"] = "other"

    @property
    def identity(self) -> str:
        normalized_name = _normalize_identifier(self.name)
        normalized_host = _normalize_identifier(self.host_api)
        if normalized_host:
            return f"{normalized_name}::{normalized_host}"
        return normalized_name

@dataclass(frozen=True)
class AudioCapture:
    wav_bytes: bytes
    pcm_bytes: bytes
    frames: int
    duration_seconds: float
    rms: float
    peak: float


_HEADSET_KEYWORDS = (
    "headset",
    "headphone",
    "earphone",
    "earbuds",
    "airpods",
    "bluetooth",
    "bluez",
    "hands free",
    "handsfree",
    "hfp",
    "a2dp",
    "usb headset",
)

_INTERNAL_KEYWORDS = (
    "built in",
    "builtin",
    "internal",
    "interno",
    "microphone array",
    "array",
    "notebook",
    "laptop",
    "hda intel",
    "sof hda",
)


def _classify_input_device(
    name: str, host_api: str = ""
) -> Literal["headset", "internal", "other"]:
    text = _normalize_identifier(f"{name} {host_api}")
    for keyword in _HEADSET_KEYWORDS:
        if keyword in text:
            return "headset"
    for keyword in _INTERNAL_KEYWORDS:
        if keyword in text:
            return "internal"
    return "other"


def choose_input_device(
    devices: tuple[AudioDevice, ...],
    *,
    remembered_identity: str | None = None,
    current_identity: str | None = None,
) -> AudioDevice | None:
    if not devices:
        return None

    for device in devices:
        if device.kind == "headset":
            return device

    if current_identity is not None:
        for device in devices:
            if device.identity == current_identity:
                return device

    if remembered_identity is not None:
        for device in devices:
            if device.identity == remembered_identity:
                return device

    for device in devices:
        if device.kind == "internal":
            return device

    for device in devices:
        if device.is_default:
            return device

    return devices[0]


def list_input_devices() -> tuple[AudioDevice, ...]:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise AudioRecorderError(
            "PortAudio não está disponível. Instale o runtime libportaudio2."
        ) from exc

    try:
        queried = sd.query_devices()
        if isinstance(queried, dict):
            queried = [queried]
        default_device = sd.default.device
        try:
            default_input = int(default_device[0])
        except (TypeError, IndexError, ValueError):
            default_input = int(default_device)

        host_api_names: dict[int, str] = {}
        if getattr(sd, "query_hostapis", None) is not None:
            try:
                hostapis = sd.query_hostapis()
                if isinstance(hostapis, (list, tuple)):
                    for position, hostapi_info in enumerate(hostapis):
                        if isinstance(hostapi_info, dict):
                            ha_name = str(hostapi_info.get("name", ""))
                            ha_index = int(hostapi_info.get("index", position))
                            host_api_names[ha_index] = ha_name
                elif isinstance(hostapis, dict):
                    ha_name = str(hostapis.get("name", ""))
                    ha_index = int(hostapis.get("index", 0))
                    host_api_names[ha_index] = ha_name
            except Exception:
                host_api_names = {}

        devices: list[AudioDevice] = []
        for position, info in enumerate(queried):
            if not isinstance(info, dict):
                continue
            max_input_channels = int(info.get("max_input_channels", 0))
            name = str(info.get("name", f"Dispositivo {position}"))
            if max_input_channels <= 0 or name.lower().endswith(".monitor"):
                continue
            index = int(info.get("index", position))
            if getattr(sd, "check_input_settings", None) is not None:
                try:
                    _resolve_sample_rate(sd, index)
                except AudioRecorderError:
                    continue

            host_api_value = info.get("hostapi")
            host_api_name = ""
            if isinstance(host_api_value, int) and host_api_value in host_api_names:
                host_api_name = host_api_names[host_api_value]
            elif isinstance(host_api_value, str):
                host_api_name = host_api_value

            kind = _classify_input_device(name, host_api_name)
            devices.append(
                AudioDevice(
                    index=index,
                    name=name,
                    max_input_channels=max_input_channels,
                    is_default=index == default_input,
                    host_api=host_api_name,
                    kind=kind,
                )
            )
        return tuple(devices)
    except Exception as exc:
        if isinstance(exc, AudioRecorderError):
            raise
        raise AudioRecorderError(
            "Não foi possível detectar os microfones disponíveis."
        ) from exc

def _resolve_sample_rate(sd: Any, device: int | str | None) -> int:
    try:
        info = sd.query_devices(device, "input")
    except TypeError:
        info = sd.query_devices(device)
    except Exception as exc:
        raise AudioRecorderError(
            "Não foi possível consultar o formato do microfone."
        ) from exc
    default_rate = int(round(float(info.get("default_samplerate", 0)))) if isinstance(info, dict) else 0
    candidates = (SAMPLE_RATE, default_rate, *_SAMPLE_RATE_CANDIDATES)
    checked: set[int] = set()
    for rate in candidates:
        if rate <= 0 or rate in checked:
            continue
        checked.add(rate)
        try:
            sd.check_input_settings(
                device=device,
                samplerate=rate,
                channels=CHANNELS,
                dtype="int16",
            )
        except Exception:
            continue
        return rate
    raise AudioRecorderError(
        "O microfone selecionado não aceita um formato de captura compatível."
    )


def _default_stream_factory(**kwargs: Any) -> Any:
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:
        raise AudioRecorderError(
            "PortAudio não está disponível. Instale o runtime libportaudio2."
        ) from exc
    return sd.InputStream(**kwargs)

class AudioRecorderError(RuntimeError):
    """Erro recuperável ao iniciar ou finalizar uma gravação."""


class AudioRecorder:
    def __init__(
        self,
        stream_factory: Any | None = None,
        device: int | str | None = None,
    ) -> None:
        self._uses_default_stream_factory = stream_factory is None
        self._stream_factory = stream_factory or _default_stream_factory
        self._device = device
        self._capture_sample_rate = SAMPLE_RATE
        self._stream: Any | None = None
        self._chunks: list[bytes] = []
        self._status: str | None = None
        self._last_capture: AudioCapture | None = None
        self._lock = threading.Lock()

    def set_device(self, device: int | str | None) -> None:
        with self._lock:
            if self._stream is not None:
                raise AudioRecorderError(
                    "Não é possível trocar o microfone durante a gravação."
                )
            self._device = device

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise AudioRecorderError("Já existe uma gravação em andamento.")
            self._chunks = []
            self._status = None
            self._last_capture = None
            device = self._device

        sample_rate = SAMPLE_RATE
        if self._uses_default_stream_factory:
            try:
                import sounddevice as sd
            except (ImportError, OSError) as exc:
                raise AudioRecorderError(
                    "PortAudio não está disponível. Instale o runtime libportaudio2."
                ) from exc
            sample_rate = _resolve_sample_rate(sd, device)

        stream = None
        try:
            stream = self._stream_factory(
                device=device,
                samplerate=sample_rate,
                channels=CHANNELS,
                dtype="int16",
                callback=self._callback,
            )
            stream.start()
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            raise AudioRecorderError("Não foi possível acessar o microfone.") from exc

        with self._lock:
            self._capture_sample_rate = sample_rate
            self._stream = stream

    def stop(self) -> AudioCapture:
        with self._lock:
            stream = self._stream
            self._stream = None

        if stream is None:
            raise AudioRecorderError("Nenhuma gravação está em andamento.")

        stop_error: AudioRecorderError | None = None
        try:
            stream.stop()
        except Exception:
            stop_error = AudioRecorderError(
                "Não foi possível parar o microfone."
            )
        close_error: AudioRecorderError | None = None
        try:
            stream.close()
        except Exception:
            close_error = AudioRecorderError(
                "Não foi possível fechar o microfone."
            )

        with self._lock:
            pcm_bytes = b"".join(self._chunks)
            sample_rate = self._capture_sample_rate
            status = self._status

        capture = _build_capture(pcm_bytes, sample_rate)
        with self._lock:
            self._last_capture = capture

        if not pcm_bytes:
            raise AudioRecorderError("Nenhum áudio foi capturado.")
        if capture.rms < MIN_RMS_LEVEL:
            raise AudioRecorderError(
                "O áudio capturado está muito baixo. Verifique o microfone e tente novamente."
            )
        if status:
            raise AudioRecorderError(
                "O áudio perdeu trechos durante a captura. Grave novamente."
            )
        if stop_error is not None:
            raise stop_error
        if close_error is not None:
            raise close_error
        return capture
    def is_recording(self) -> bool:
        with self._lock:
            return self._stream is not None

    def last_status(self) -> str | None:
        with self._lock:
            return self._status

    def last_capture(self) -> AudioCapture | None:
        with self._lock:
            return self._last_capture

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info
        status_text = str(status) if status else None
        chunk = indata.copy().tobytes()
        with self._lock:
            if status_text:
                self._status = status_text
            self._chunks.append(chunk)


def _resample_pcm(pcm_bytes: bytes, source_rate: int) -> bytes:
    if source_rate == SAMPLE_RATE:
        return pcm_bytes
    try:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    except ValueError as exc:
        raise AudioRecorderError("O áudio capturado está inválido.") from exc
    if samples.size == 0:
        return pcm_bytes
    target_frames = max(1, round(samples.size * SAMPLE_RATE / source_rate))
    source_positions = np.arange(samples.size, dtype=np.float64)
    target_positions = np.linspace(
        0,
        samples.size - 1,
        target_frames,
        dtype=np.float64,
    )
    resampled = np.interp(
        target_positions,
        source_positions,
        samples.astype(np.float64),
    )
    return np.rint(np.clip(resampled, -32768, 32767)).astype(np.int16).tobytes()


def _build_capture(
    pcm_bytes: bytes,
    source_rate: int = SAMPLE_RATE,
) -> AudioCapture:
    if not pcm_bytes:
        return AudioCapture(
            wav_bytes=b"",
            pcm_bytes=b"",
            frames=0,
            duration_seconds=0.0,
            rms=0.0,
            peak=0.0,
        )
    pcm_bytes = _resample_pcm(pcm_bytes, source_rate)
    try:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    except ValueError as exc:
        raise AudioRecorderError("O áudio capturado está inválido.") from exc
    if samples.size == 0:
        return AudioCapture(
            wav_bytes=b"",
            pcm_bytes=pcm_bytes,
            frames=0,
            duration_seconds=0.0,
            rms=0.0,
            peak=0.0,
        )
    normalized = samples.astype(np.float64) / 32768.0
    return AudioCapture(
        wav_bytes=serialize_wav(pcm_bytes),
        pcm_bytes=pcm_bytes,
        frames=int(samples.size),
        duration_seconds=float(samples.size / SAMPLE_RATE),
        rms=float(np.sqrt(np.mean(normalized * normalized))),
        peak=float(np.max(np.abs(normalized))),
    )


def serialize_wav(pcm_bytes: bytes) -> bytes:
    if not pcm_bytes:
        raise AudioRecorderError("Nenhum áudio foi capturado.")

    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm_bytes)
    return output.getvalue()

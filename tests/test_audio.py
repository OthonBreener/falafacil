from __future__ import annotations

import io
import sys
import types
import wave

import numpy as np
import pytest

from falafacil.audio import (
    AudioDevice,
    AudioRecorder,
    AudioRecorderError,
    MIN_RMS_LEVEL,
    serialize_wav,
)

class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs.get("callback")
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def test_serialize_wav_has_expected_audio_format() -> None:
    pcm = b"\x01\x00\x02\x00"

    result = serialize_wav(pcm)

    with wave.open(io.BytesIO(result), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.readframes(wav_file.getnframes()) == pcm


def test_recorder_collects_metrics_device_and_closes_stream() -> None:
    streams: list[FakeStream] = []

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    recorder = AudioRecorder(stream_factory=factory, device=7)
    recorder.start()
    streams[0].callback(
        np.array([[1000], [-1000]], dtype=np.int16),
        2,
        None,
        None,
    )

    result = recorder.stop()

    assert result.wav_bytes.startswith(b"RIFF")
    assert result.pcm_bytes == np.array([[1000], [-1000]], dtype=np.int16).tobytes()
    assert result.frames == 2
    assert result.duration_seconds == pytest.approx(2 / 16_000)
    assert result.rms == pytest.approx(1000 / 32768)
    assert result.peak == pytest.approx(1000 / 32768)
    assert streams[0].kwargs["device"] == 7
    assert streams[0].started
    assert streams[0].stopped
    assert streams[0].closed
    assert not recorder.is_recording()


def test_recorder_blocks_device_change_during_recording() -> None:
    stream = FakeStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream, device=1)
    recorder.start()
    stream.callback = recorder._callback

    with pytest.raises(AudioRecorderError, match="trocar o microfone"):
        recorder.set_device(2)

    stream.callback(np.array([[1000]], dtype=np.int16), 1, None, None)
    recorder.stop()


def test_recorder_keeps_low_capture_for_diagnostics() -> None:
    stream = FakeStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream)
    recorder.start()
    stream.callback = recorder._callback
    stream.callback(np.array([[1], [-1]], dtype=np.int16), 2, None, None)

    with pytest.raises(AudioRecorderError, match="muito baixo"):
        recorder.stop()

    assert recorder.last_capture() is not None
    assert recorder.last_capture().rms < MIN_RMS_LEVEL


def test_recorder_keeps_empty_capture_and_closes_stream() -> None:
    stream = FakeStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream)
    recorder.start()

    with pytest.raises(AudioRecorderError, match="Nenhum áudio"):
        recorder.stop()

    assert recorder.last_capture() is not None
    assert recorder.last_capture().frames == 0
    assert stream.closed


def test_recorder_rejects_duplicate_start() -> None:
    stream = FakeStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream)
    with pytest.raises(AudioRecorderError, match="Nenhuma gravação"):
        recorder.stop()

    recorder.start()
    with pytest.raises(AudioRecorderError, match="Já existe"):
        recorder.start()


def test_list_input_devices_filters_inputs_marks_array_default_and_monitors(
    monkeypatch,
) -> None:
    fake_sounddevice = types.SimpleNamespace(
        query_devices=lambda: [
            {"index": 0, "name": "Saída", "max_input_channels": 0},
            {"index": 1, "name": "Microfone USB", "max_input_channels": 1},
            {"index": 2, "name": "Microfone interno", "max_input_channels": 2},
            {"index": 3, "name": "alto-falante.monitor", "max_input_channels": 2},
        ],
        default=types.SimpleNamespace(device=np.array([2, 0])),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    from falafacil.audio import list_input_devices

    assert list_input_devices() == (
        AudioDevice(1, "Microfone USB", 1, False),
        AudioDevice(2, "Microfone interno", 2, True, kind="internal"),
    )


def test_recorder_uses_supported_native_rate_and_resamples(monkeypatch) -> None:
    streams: list[FakeStream] = []
    calls: list[int] = []

    def query_devices(device=None, kind=None):
        del kind
        return {"default_samplerate": 48_000.0} if device is not None else []

    def check_input_settings(*, device, samplerate, channels, dtype):
        del device, channels, dtype
        calls.append(samplerate)
        if samplerate != 48_000:
            raise RuntimeError("taxa não suportada")

    def input_stream(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    fake_sounddevice = types.SimpleNamespace(
        query_devices=query_devices,
        check_input_settings=check_input_settings,
        InputStream=input_stream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    recorder = AudioRecorder(device=4)
    recorder.start()
    streams[0].callback(
        np.full((6, 1), 1000, dtype=np.int16),
        6,
        None,
        None,
    )
    capture = recorder.stop()

    assert streams[0].kwargs["samplerate"] == 48_000
    assert calls[:2] == [16_000, 48_000]
    assert capture.frames == 2
    assert capture.wav_bytes.startswith(b"RIFF")


def test_list_input_devices_allows_empty_result(monkeypatch) -> None:
    fake_sounddevice = types.SimpleNamespace(
        query_devices=lambda: [
            {"index": 0, "name": "Saída", "max_input_channels": 0},
        ],
        default=types.SimpleNamespace(device=np.array([0, 0])),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sounddevice)

    from falafacil.audio import list_input_devices

    assert list_input_devices() == ()



def test_resampling_preserves_capture_duration() -> None:
    from falafacil.audio import _build_capture

    capture = _build_capture(
        np.full((96_000, 1), 1000, dtype=np.int16).tobytes(),
        48_000,
    )

    assert capture.frames == 32_000
    assert capture.duration_seconds == pytest.approx(2.0)
    with wave.open(io.BytesIO(capture.wav_bytes), "rb") as wav_file:
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnframes() == 32_000


def test_normalize_identifier_and_classification_across_separator_variants() -> None:
    from falafacil.audio import _classify_input_device, _normalize_identifier

    assert _normalize_identifier("Hands_Free") == "hands free"
    assert _normalize_identifier("hands-free") == "hands free"
    assert _normalize_identifier("  hands---free__device  ") == "hands free device"
    assert _normalize_identifier("Microphone_Array") == "microphone array"
    assert _normalize_identifier("Microfone Integrado (Áudio)") == "microfone integrado audio"

    # Headset classification with separator variants
    assert _classify_input_device("Hands_Free") == "headset"
    assert _classify_input_device("hands-free") == "headset"
    assert _classify_input_device("hands free") == "headset"
    assert _classify_input_device("HFP/A2DP Headset", "ALSA") == "headset"
    assert _classify_input_device("USB_Headset", "PulseAudio") == "headset"
    assert _classify_input_device("Bluetooth Earbuds") == "headset"

    # Internal classification with separator variants
    assert _classify_input_device("Microphone_Array") == "internal"
    assert _classify_input_device("microphone-array") == "internal"
    assert _classify_input_device("Microphone Array") == "internal"
    assert _classify_input_device("Built-in Audio") == "internal"
    assert _classify_input_device("Built_in Audio") == "internal"
    assert _classify_input_device("sof-hda-dsp") == "internal"
    assert _classify_input_device("HDA-Intel PCH") == "internal"

    # Ambiguous remains other
    assert _classify_input_device("USB Audio") == "other"
    assert _classify_input_device("Line In") == "other"
    assert _classify_input_device("Microfone Genérico") == "other"


def test_audio_device_identity_stability_across_separator_variants() -> None:
    dev1 = AudioDevice(0, "Hands_Free", 1, False, host_api="ALSA_Audio")
    dev2 = AudioDevice(1, "hands-free", 1, False, host_api="alsa-audio")
    dev3 = AudioDevice(2, "hands free", 1, False, host_api="ALSA Audio")

    assert dev1.identity == "hands free::alsa audio"
    assert dev1.identity == dev2.identity == dev3.identity

    internal1 = AudioDevice(0, "Microphone_Array", 2, True)
    internal2 = AudioDevice(1, "Microphone-Array", 2, True)
    internal3 = AudioDevice(2, "Microphone Array", 2, True)

    assert internal1.identity == "microphone array"
    assert internal1.identity == internal2.identity == internal3.identity


def test_choose_input_device_priority_order() -> None:
    from falafacil.audio import choose_input_device

    headset = AudioDevice(1, "Headset Bluetooth", 1, False, kind="headset")
    internal = AudioDevice(2, "Built-in Mic", 2, False, kind="internal")
    default_dev = AudioDevice(3, "USB Mic", 1, True, kind="other")
    other_dev = AudioDevice(4, "Generic Mic", 1, False, kind="other")

    devices = (other_dev, default_dev, internal, headset)

    # 1. Headset wins even if another device is remembered/current/default
    assert (
        choose_input_device(
            devices,
            remembered_identity=internal.identity,
            current_identity=default_dev.identity,
        )
        == headset
    )

    # 2. When headset is absent, current identity wins
    devices_no_headset = (other_dev, default_dev, internal)
    assert (
        choose_input_device(
            devices_no_headset,
            remembered_identity=internal.identity,
            current_identity=default_dev.identity,
        )
        == default_dev
    )

    # 3. When headset and current are absent, remembered identity wins
    assert (
        choose_input_device(
            devices_no_headset,
            remembered_identity=internal.identity,
        )
        == internal
    )

    # 4. When no headset, current, or remembered: internal wins over default/other
    assert choose_input_device(devices_no_headset) == internal

    # 5. When no internal: default wins
    assert choose_input_device((other_dev, default_dev)) == default_dev

    # 6. Fallback to first
    assert choose_input_device((other_dev,)) == other_dev

    # 7. Empty list returns None
    assert choose_input_device(()) is None


def test_recorder_start_failure_omits_raw_exception_and_secret() -> None:
    secret = "secret-token-mic-start-1234"

    class FailingStartStream(FakeStream):
        def start(self):
            raise RuntimeError(f"ALSA start fault with {secret}")

    recorder = AudioRecorder(stream_factory=lambda **kwargs: FailingStartStream(**kwargs))
    with pytest.raises(AudioRecorderError) as exc_info:
        recorder.start()

    message = str(exc_info.value)
    assert message == "Não foi possível acessar o microfone."
    assert secret not in message
    assert "RuntimeError" not in message
    assert "ALSA" not in message


def test_recorder_stop_failure_omits_raw_exception_and_secret_and_closes_stream() -> None:
    secret = "secret-token-mic-stop-5678"
    closed_called = False

    class FailingStopStream(FakeStream):
        def stop(self):
            raise RuntimeError(f"ALSA stop fault with {secret}")

        def close(self):
            nonlocal closed_called
            closed_called = True
            super().close()

    stream = FailingStopStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream)
    recorder.start()

    with pytest.raises(AudioRecorderError) as exc_info:
        recorder.stop()

    message = str(exc_info.value)
    assert message == "Não foi possível parar o microfone."
    assert secret not in message
    assert "RuntimeError" not in message
    assert closed_called is True
    assert stream.closed is True


def test_recorder_close_failure_omits_raw_exception_and_secret() -> None:
    secret = "secret-token-mic-close-9012"

    class FailingCloseStream(FakeStream):
        def close(self):
            raise RuntimeError(f"ALSA close fault with {secret}")

    stream = FailingCloseStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream)
    recorder.start()

    with pytest.raises(AudioRecorderError) as exc_info:
        recorder.stop()

    message = str(exc_info.value)
    assert message == "Não foi possível fechar o microfone."
    assert secret not in message
    assert "RuntimeError" not in message


def test_recorder_both_stop_and_close_failure_preserves_order_and_omits_secret() -> None:
    stop_secret = "secret-token-mic-both-stop-1111"
    close_secret = "secret-token-mic-both-close-2222"
    calls: list[str] = []

    class FailingBothStream(FakeStream):
        def stop(self):
            calls.append("stop")
            raise RuntimeError(f"stop fault with {stop_secret}")

        def close(self):
            calls.append("close")
            raise RuntimeError(f"close fault with {close_secret}")

    stream = FailingBothStream()
    recorder = AudioRecorder(stream_factory=lambda **kwargs: stream)
    recorder.start()

    with pytest.raises(AudioRecorderError) as exc_info:
        recorder.stop()

    message = str(exc_info.value)
    assert message == "Não foi possível parar o microfone."
    assert stop_secret not in message
    assert close_secret not in message
    assert calls == ["stop", "close"]

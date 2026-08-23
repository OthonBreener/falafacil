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
        AudioDevice(2, "Microfone interno", 2, True),
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

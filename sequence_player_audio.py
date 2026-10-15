# sequence_player_audio.py
# Plays {sequence_id}_previewaudio.wav with QAudioSink.
# PCM is read with Python wave, held in memory, and pulled via QBuffer.
# During play the timeline uses position_ms() as the master clock.
# ASCII only.

import os
import wave


SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2  # Int16


def _qt_audio():
    # Lazy import so helpers load without a display/EGL stack.
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtMultimedia import QAudioFormat, QAudioSink
    return QBuffer, QByteArray, QIODevice, QAudioFormat, QAudioSink


def sequence_frame_from_position_ms(position_ms, fps):
    # Absolute sequence frame for a full-sequence WAV.
    # frame = position_ms * fps / 1000. Returns None if position is unusable.
    if position_ms is None:
        return None
    try:
        pos = int(position_ms)
    except Exception:
        return None
    if pos < 0:
        return None
    try:
        fps = float(fps)
    except Exception:
        fps = 29.97
    if fps <= 0:
        fps = 29.97
    return int(pos * fps / 1000.0)


def _read_wav_pcm(path):
    # Return (pcm_bytes, sample_rate, channels, sample_width) or None.
    try:
        with wave.open(path, "rb") as w:
            rate = int(w.getframerate())
            ch = int(w.getnchannels())
            width = int(w.getsampwidth())
            n = int(w.getnframes())
            pcm = w.readframes(n)
    except Exception:
        return None
    if width != SAMPLE_WIDTH or ch < 1:
        return None
    return pcm, rate, ch, width


def byte_offset_for_frame(frame, fps, sample_rate=SAMPLE_RATE, width=SAMPLE_WIDTH,
                          channels=CHANNELS):
    # Sample index for sequence frame, then byte offset aligned to frame size.
    try:
        fps = float(fps)
    except Exception:
        fps = 29.97
    if fps <= 0:
        fps = 29.97
    sample = int(round(float(frame) * float(sample_rate) / fps))
    if sample < 0:
        sample = 0
    frame_bytes = int(width) * int(channels)
    if frame_bytes < 1:
        frame_bytes = 2
    return int(sample * frame_bytes)


class PreviewAudioPlayer(object):
    # QAudioSink + in-memory PCM. Owned by SequencePlayer.

    def __init__(self):
        self.wav_path = None
        self.fps = 29.97
        self._pcm = b""
        self._qba = None
        self._buffer = None
        self._sink = None
        self._format = None
        self._QAudioSink = None
        self._start_offset_ms = 0
        self._active = False
        self._sample_rate = SAMPLE_RATE
        self._channels = CHANNELS
        self._width = SAMPLE_WIDTH

    def has_audio(self):
        return bool(self._pcm) and self.wav_path is not None

    def prepare(self, wav_path, fps):
        self.fps = float(fps) if fps and float(fps) > 0 else 29.97
        path = os.path.abspath(wav_path) if wav_path else None
        if not path or not os.path.isfile(path):
            self.stop()
            self.wav_path = None
            self._pcm = b""
            self._qba = None
            self._buffer = None
            return False
        if path == self.wav_path and self._pcm and self._buffer is not None:
            return True
        loaded = _read_wav_pcm(path)
        if not loaded:
            self.wav_path = None
            self._pcm = b""
            return False
        pcm, rate, ch, width = loaded
        self.stop()
        self._pcm = pcm
        self._sample_rate = rate
        self._channels = ch
        self._width = width
        QBuffer, QByteArray, QIODevice, QAudioFormat, QAudioSink = _qt_audio()
        self._QAudioSink = QAudioSink
        self._qba = QByteArray(pcm)
        self._buffer = QBuffer(self._qba)
        if not self._buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            self._buffer = None
            self._pcm = b""
            self.wav_path = None
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._format = fmt
        self.wav_path = path
        self._start_offset_ms = 0
        return True

    def _frame_ms(self, frame):
        return int(round(float(frame) * 1000.0 / self.fps))

    def _byte_pos(self, frame):
        pos = byte_offset_for_frame(
            frame, self.fps, self._sample_rate, self._width, self._channels)
        step = max(2, int(self._width) * int(self._channels))
        pos = pos - (pos % step)
        if pos < 0:
            pos = 0
        n = len(self._pcm)
        if pos > n:
            pos = n - (n % step)
            if pos < 0:
                pos = 0
        return pos

    def _ensure_sink(self):
        if self._sink is not None:
            return self._sink
        if self._format is None:
            return None
        if self._QAudioSink is None:
            _qb, _qa, _qi, _qf, QAudioSink = _qt_audio()
            self._QAudioSink = QAudioSink
        self._sink = self._QAudioSink(self._format)
        return self._sink

    def start(self, frame):
        self.play_at_frame(frame)

    def play_at_frame(self, frame):
        if not self.has_audio() or self._buffer is None:
            return
        frame = int(frame)
        self._start_offset_ms = self._frame_ms(frame)
        sink = self._ensure_sink()
        if sink is None:
            return
        try:
            sink.stop()
        except Exception:
            pass
        self._buffer.seek(self._byte_pos(frame))
        try:
            sink.start(self._buffer)
            self._active = True
        except Exception:
            self._active = False

    def seek(self, frame):
        self.seek_frame(frame)

    def seek_frame(self, frame):
        if not self.has_audio() or self._buffer is None:
            return
        frame = int(frame)
        was = self._active
        self._start_offset_ms = self._frame_ms(frame)
        sink = self._sink
        if sink is not None:
            try:
                sink.stop()
            except Exception:
                pass
        self._buffer.seek(self._byte_pos(frame))
        if was and sink is not None:
            try:
                sink.start(self._buffer)
                self._active = True
            except Exception:
                self._active = False

    def position_ms(self):
        # start_offset_ms + processedUSecs()/1000. None if no WAV.
        if not self.has_audio():
            return None
        extra = 0
        if self._sink is not None and self._active:
            try:
                extra = int(self._sink.processedUSecs()) // 1000
            except Exception:
                extra = 0
        return int(self._start_offset_ms) + int(extra)

    def pause(self):
        if self._sink is None:
            return
        try:
            self._sink.suspend()
        except Exception:
            pass

    def resume(self):
        if self._sink is None:
            return
        try:
            self._sink.resume()
        except Exception:
            pass

    def stop(self):
        self._active = False
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass
        self._start_offset_ms = 0

    def resync_if_drift(self, frame, threshold_ms=80):
        # Neutralized: audio is the play-clock master. Kept as a no-op.
        return

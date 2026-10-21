# sequence_player_audio.py
# Plays {ProjectName}_{sequence_id}_PreviewAudio.wav with QAudioSink.
# PCM is read with Python wave, held in memory, and pulled via QBuffer.
# During play the timeline uses position_ms() as the master clock.
# ASCII only.

import os
import time
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
        self._wav_stamp = None
        self._wav_gen = None     # BUILD 135: mix generation of the PCM
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

    def prepare(self, wav_path, fps, gen=None):
        # gen (BUILD 135): sequence_preview_audio generation; a different
        # value reloads the PCM even when path / size / mtime look equal
        self.fps = float(fps) if fps and float(fps) > 0 else 29.97
        path = os.path.abspath(wav_path) if wav_path else None
        if not path or not os.path.isfile(path):
            self.stop()
            self.wav_path = None
            self._pcm = b""
            self._qba = None
            self._buffer = None
            return False
        # Reuse the in-memory PCM only if the WAV on disk is the same file
        # (path, size, mtime). A rebuilt mix (gain, slice, move) keeps the
        # same path, so the path alone would replay the old audio.
        try:
            st = os.stat(path)
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError:
            stamp = None
        if (path == self.wav_path and self._pcm and self._buffer is not None
                and stamp is not None and stamp == self._wav_stamp
                and (gen is None or gen == self._wav_gen)):
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
        self._wav_stamp = stamp
        self._wav_gen = gen
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


# ---------------------------------------------------------------- BUILD 111
# Scrub audio burst: about one frame of the existing preview mix WAV at the
# new playhead when the ruler is clicked / dragged while stopped. Never
# builds the mix (no .sig check: a stale WAV is fine), never touches the
# play sink, the play clock or the preview video path.

BURST_RAMP_SAMPLES = 32     # short linear fade in / out against clicks
BURST_POLL_MS = 5           # BUILD 113: burst-done check interval
BURST_DONE_MARGIN_S = 0.03  # drained buffer + this = done (output latency)
BURST_HARD_CAP_S = 0.25     # done regardless, never stuck waiting


def burst_slice_bounds(frame, fps, n_bytes, sample_rate=SAMPLE_RATE,
                       channels=CHANNELS, width=SAMPLE_WIDTH):
    # (start_byte, end_byte) of one frame of audio (round(rate / fps)
    # samples) at sequence frame, block aligned and clipped to n_bytes.
    # (0, 0) or an empty range when the frame is past the end.
    try:
        fps = float(fps)
    except Exception:
        fps = 29.97
    if fps <= 0:
        fps = 29.97
    block = max(1, int(width) * int(channels))
    start = byte_offset_for_frame(frame, fps, sample_rate, width, channels)
    start -= start % block
    count = max(1, int(round(float(sample_rate) / fps)))
    end = start + count * block
    limit = int(n_bytes) - (int(n_bytes) % block)
    start = max(0, min(start, limit))
    end = max(start, min(end, limit))
    return start, end


def ramp_pcm16(data, channels=CHANNELS, ramp=BURST_RAMP_SAMPLES):
    # Linear fade in over the first ramp sample frames and out over the
    # last ramp, Int16 little-endian interleaved. Returns new bytes.
    import array
    import sys
    a = array.array("h")
    a.frombytes(bytes(data))
    if sys.byteorder != "little":
        a.byteswap()
    ch = max(1, int(channels))
    frames = len(a) // ch
    r = min(int(ramp), frames // 2)
    for i in range(r):
        g = float(i) / float(r)
        for c in range(ch):
            a[i * ch + c] = int(a[i * ch + c] * g)
            j = (frames - 1 - i) * ch + c
            a[j] = int(a[j] * g)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


class ScrubBurst(object):
    # BUILD 112: ONE long-lived QAudioSink and ONE QBuffer (internal data,
    # set with setData, so no external QByteArray to outlive), both
    # parented to a QObject owner created on the GUI thread and reused.
    # release() stops everything and deleteLater()s the owner.
    #
    # BUILD 111 created a new QByteArray and QBuffer per burst and dropped
    # the old ones right after sink.stop(); Python freed them at once (no
    # parent), and the old QByteArray was freed while the old QBuffer still
    # pointed at it. A sink backend still holding the old pull device can
    # then touch freed memory.
    #
    # BUILD 113: a playing burst is never cut off. A scrub that arrives
    # while one plays only stores the latest frame (overwriting any older
    # pending one; intermediate frames are dropped). When the burst is
    # done the pending frame plays, then is cleared. "Done" is checked
    # from an owner-parented polling QTimer (queued, GUI thread, no
    # deletion): sink IdleState/StoppedState, or the buffer fully pulled
    # and the burst duration plus a margin elapsed (some backends stay
    # ActiveState after draining), or a hard cap. No stateChanged slot:
    # in PySide6 6.12 the QAudio::State argument fails to convert
    # ("Cannot call meta function"), so the sink signal is not used.

    def __init__(self):
        self.path = None
        self._stamp = None
        self._gen = None                # BUILD 135: mix generation
        self._pcm = b""
        self._rate = SAMPLE_RATE
        self._ch = CHANNELS
        self._width = SAMPLE_WIDTH
        self._owner = None
        self._sink = None
        self._sink_fmt = None
        self._buf = None
        self._timer = None              # single-shot 0: start when free
        self._poll = None               # BURST_POLL_MS: done check
        self._pending = None            # (frame, data) latest request only
        self._playing = False
        self._t_start = 0.0
        self._dur = 0.0
        self._released = False
        self.last_frame = None
        self.started = 0                # bursts actually started (tests)
        self.frames = []                # started frames, kept short (tests)

    def load(self, wav_path, shared=None, gen=None):
        # Cached PCM, reloaded only when path / size / mtime change (BUILD
        # 79 rule). shared: a PreviewAudioPlayer whose PCM can be reused.
        path = os.path.abspath(wav_path) if wav_path else None
        if not path or not os.path.isfile(path):
            return False
        try:
            st = os.stat(path)
            stamp = (st.st_size, st.st_mtime_ns)
        except OSError:
            return False
        if (path == self.path and stamp == self._stamp and self._pcm
                and (gen is None or gen == self._gen)):
            return True
        if (shared is not None and getattr(shared, "wav_path", None) == path
                and getattr(shared, "_wav_stamp", None) == stamp
                and (gen is None or getattr(shared, "_wav_gen", None) == gen)
                and getattr(shared, "_pcm", b"")):
            self._pcm = shared._pcm
            self._rate, self._ch, self._width = (
                shared._sample_rate, shared._channels, shared._width)
        else:
            loaded = _read_wav_pcm(path)
            if not loaded:
                return False
            self._pcm, self._rate, self._ch, self._width = loaded
        self.path = path
        self._stamp = stamp
        self._gen = gen
        return True

    def slice_for(self, frame, fps):
        s, e = burst_slice_bounds(frame, fps, len(self._pcm), self._rate,
                                  self._ch, self._width)
        if e <= s:
            return b""
        return ramp_pcm16(self._pcm[s:e], self._ch)

    def _ensure_objects(self):
        # GUI thread, lazily; the sink is rebuilt only if the WAV format
        # changes (old one stopped and deleteLater, never deleted here)
        from PySide6.QtCore import QObject, QBuffer, QTimer
        if self._owner is None:
            self._owner = QObject()
            self._buf = QBuffer(self._owner)
            self._timer = QTimer(self._owner)
            self._timer.setSingleShot(True)
            self._timer.setInterval(0)
            self._timer.timeout.connect(self._start_pending)
            self._poll = QTimer(self._owner)
            self._poll.setInterval(BURST_POLL_MS)
            self._poll.timeout.connect(self._check_done)
        fmt_key = (int(self._rate), int(self._ch))
        if self._sink is not None and self._sink_fmt != fmt_key:
            old = self._sink
            self._sink = None
            self._playing = False
            try:
                old.stop()
                old.deleteLater()
            except Exception:
                pass
        if self._sink is None:
            _qb, _qa, _qi, QAudioFormat, QAudioSink = _qt_audio()
            fmt = QAudioFormat()
            fmt.setSampleRate(fmt_key[0])
            fmt.setChannelCount(fmt_key[1])
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            self._sink = QAudioSink(fmt, self._owner)
            self._sink_fmt = fmt_key
        return True

    def play(self, frame, fps):
        # Store the latest request; it starts at once (next event-loop
        # pass) when nothing plays, else when the current burst is done
        if self._released:
            return False
        data = self.slice_for(frame, fps)
        if not data:
            return False
        self._ensure_objects()
        self._pending = (int(frame), data)
        if not self._playing and not self._timer.isActive():
            self._timer.start()
        return True

    def _start_pending(self):
        # Timer slot only. Never called while a burst is still playing.
        if self._released or self._playing:
            return
        if self._pending is None or self._sink is None:
            return
        frame, data = self._pending
        self._pending = None
        _qb, _qa, QIODevice, _qf, _qs = _qt_audio()
        try:
            self._sink.stop()              # previous burst already done
        except Exception:
            pass
        if self._buf.isOpen():
            self._buf.close()
        self._buf.setData(data)            # QBuffer keeps its own copy
        if not self._buf.open(QIODevice.OpenModeFlag.ReadOnly):
            return
        self._buf.seek(0)
        try:
            self._sink.start(self._buf)
        except Exception:
            return
        self._playing = True
        self._t_start = time.monotonic()
        bps = float(self._rate) * self._ch * self._width
        self._dur = (len(data) / bps) if bps > 0 else 0.0
        self.last_frame = frame
        self.started += 1
        self.frames.append(frame)
        if len(self.frames) > 1000:
            del self.frames[:500]
        self._poll.start()
        # print("SCRUB: burst frame=%d bytes=%d" % (frame, len(data)))

    def _burst_done(self):
        _qb, _qa, _qi, _qf, _qs = _qt_audio()
        from PySide6.QtMultimedia import QAudio
        try:
            st = self._sink.state()
        except Exception:
            return True
        if st in (QAudio.State.IdleState, QAudio.State.StoppedState):
            return True
        el = time.monotonic() - self._t_start
        if el >= self._dur + BURST_HARD_CAP_S:
            return True
        try:
            drained = self._buf.atEnd()
        except Exception:
            drained = True
        return drained and el >= self._dur + BURST_DONE_MARGIN_S

    def _check_done(self):
        # Poll-timer slot (queued, GUI thread). No deletion here.
        if self._released or not self._playing or self._sink is None:
            if self._poll is not None:
                self._poll.stop()
            return
        if not self._burst_done():
            return
        self._poll.stop()
        self._playing = False
        if self._pending is not None:
            self._start_pending()          # latest stored frame, then cleared

    def stop(self):
        # Play start / player close: drop any pending frame, silence now
        self._pending = None
        self._playing = False
        for t in (self._timer, self._poll):
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
        if self._sink is not None:
            try:
                self._sink.stop()
            except Exception:
                pass

    def release(self):
        # Player close: stop first, then deleteLater the owner (sink,
        # buffer and timers are its children)
        self.stop()
        self._released = True
        if self._buf is not None:
            try:
                if self._buf.isOpen():
                    self._buf.close()
            except Exception:
                pass
        if self._owner is not None:
            try:
                self._owner.deleteLater()
            except Exception:
                pass
        self._owner = None
        self._sink = None
        self._buf = None
        self._timer = None
        self._poll = None


def release_scrub_burst(player):
    # BUILD 112: called when the player closes or Play starts
    burst = getattr(player, "_scrub_burst", None)
    if burst is None:
        return
    try:
        burst.release()
    except Exception:
        pass
    player._scrub_burst = None


def stop_scrub_burst(player):
    burst = getattr(player, "_scrub_burst", None)
    if burst is not None:
        try:
            burst.stop()
        except Exception:
            pass


def scrub_burst(timeline):
    # Ruler click / drag hook: one frame of mix audio at the playhead when
    # stopped and this sequence's player is open. Missing WAV: no-op.
    try:
        if getattr(timeline, "_playing", False) or getattr(timeline, "_play_filling", False):
            return False
        player = getattr(timeline, "_player", None)
        if player is None or getattr(player, "closed", True):
            return False
        frame = int(timeline.current_frame)
        from sequence_preview_audio import (preview_audio_wav_path,
                                            preview_audio_scrub_ok,
                                            preview_audio_generation)
        project_db = timeline._project_db()
        fps = float(getattr(timeline, "fps", 30) or 30)
        wav = preview_audio_wav_path(project_db, timeline.sequence_id)
        if not wav or not os.path.isfile(wav):
            return False
        # BUILD 135: silent while the mix is stale (Play rebuilds it)
        if not preview_audio_scrub_ok(project_db, timeline.sequence_id, fps,
                                      int(timeline.total_frames())):
            stop_scrub_burst(player)
            return False
        gen = preview_audio_generation(project_db, timeline.sequence_id)
        burst = getattr(player, "_scrub_burst", None)
        if burst is None:
            burst = ScrubBurst()
            player._scrub_burst = burst
        if not burst.load(wav, getattr(player, "_preview_audio", None), gen):
            return False
        return burst.play(frame, fps)
    except Exception as e:
        # print("scrub burst failed: %s" % e)
        return False

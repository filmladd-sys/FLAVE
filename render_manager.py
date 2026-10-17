# render_manager.py
# BUILD 116: Render Manager, first version. MP4 only (H.264 High + AAC,
# optional mov_text captions). Whole sequence, no progress bar, no in/out.
# One window per sequence, opened from the Sequence Timeline 'Render' menu.
# Video: the preview composite (timeline_snapshot(tl, use_proxies=False),
# same CUDA blend_layer, scale / position / opacity keyframes) run on the
# open Sequence Player's CUDA context from a render thread, raw RGB frames
# copied to the host and piped to ffmpeg (rawvideo rgb24); ffmpeg converts
# to yuv420p. Captions are not burned in.
# Audio: the preview mix code (audio tracks, gain_db, volume keyframes)
# rendered fresh into a temp WAV, mono or stereo (duplicated).
# ASCII only.

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from fractions import Fraction

SAMPLE_RATE = 48000
AAC_STEREO = "192k"
AAC_MONO = "128k"
PLAYER_WAIT_S = 15.0        # wait for the Sequence Player GPU setup
DEFAULT_GOP = 15
DEFAULT_TARGET_MBPS = 5.0
DEFAULT_BURST_MBPS = 10.0

_open_windows = {}          # sequence_id -> RenderManagerWindow
_encoder_cache = {}         # "h264" -> encoder name chosen this session

# 2-letter -> ISO 639-2 for the MP4 language tag (3-letter kept as is)
_LANG3 = {"en": "eng", "fr": "fra", "es": "spa", "de": "deu", "it": "ita",
          "pt": "por", "nl": "nld", "ja": "jpn", "zh": "zho", "ko": "kor",
          "ru": "rus", "ar": "ara", "hi": "hin", "sv": "swe", "no": "nor",
          "da": "dan", "fi": "fin", "pl": "pol", "tr": "tur", "el": "ell",
          "he": "heb", "cs": "ces", "hu": "hun", "uk": "ukr"}


def _no_window_flags():
    return 0x08000000 if sys.platform.startswith("win") else 0  # CREATE_NO_WINDOW


# ---------------------------------------------------------------- pure helpers

def fps_rational(fps):
    # ffmpeg -framerate text: NTSC rates as N*1000/1001, whole rates as int
    try:
        f = float(fps)
    except (TypeError, ValueError):
        f = 30.0
    if f <= 0:
        f = 30.0
    r = round(f)
    if abs(f - r) < 1e-6:
        return "%d" % r
    ntsc = round(f * 1001.0 / 1000.0)
    if ntsc > 0 and abs(ntsc * 1000.0 / 1001.0 - f) < 0.005:
        return "%d/1001" % (ntsc * 1000)
    fr = Fraction(f).limit_denominator(1001)
    return "%d/%d" % (fr.numerator, fr.denominator)


def fps_float(rate_text):
    if "/" in rate_text:
        a, b = rate_text.split("/", 1)
        return float(a) / float(b)
    return float(rate_text)


def _sanitize(name):
    try:
        from window_snapshot import sanitize_filename
        return sanitize_filename(name, default="sequence")
    except Exception:
        bad = '<>:"/\\|?*'
        s = "".join("_" if (c in bad or ord(c) < 32) else c for c in (name or ""))
        return s.strip(" ._") or "sequence"


def default_output_name(sequence_name):
    return "%s_v01.mp4" % _sanitize(sequence_name)


def resolve_output_path(text, project_db):
    # Absolute: as is. Bare name or relative path: under the project folder
    # (the folder holding the project .db). No other validation.
    p = os.path.expanduser((text or "").strip())
    if not p:
        return ""
    if os.path.isabs(p):
        return os.path.normpath(p)
    base = os.path.dirname(os.path.abspath(project_db)) if project_db else os.getcwd()
    return os.path.normpath(os.path.join(base, p))


def mbps_text(v):
    return "%gM" % float(v)


def language_tag(languages):
    # One ISO 639-2 code if every caption track with captions agrees, else None
    codes = set()
    for lang in languages:
        s = (lang or "").strip().lower()
        if not s:
            continue
        s = s.replace("_", "-").split("-")[0]
        if len(s) == 2:
            s = _LANG3.get(s, "")
        if len(s) == 3 and s.isalpha():
            codes.add(s)
        else:
            return None
    return codes.pop() if len(codes) == 1 else None


def _srt_time(seconds):
    ms = int(round(max(0.0, seconds) * 1000.0))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def srt_text(captions, fps):
    # captions: (start_frame, length_frames, text). Times in sequence
    # seconds; overlaps allowed. Empty text / non-positive length skipped.
    try:
        fps = float(fps) if fps and float(fps) > 0 else 30.0
    except (TypeError, ValueError):
        fps = 30.0
    out = []
    n = 0
    for start, length, text in sorted(captions, key=lambda c: (c[0], c[1])):
        body = "\n".join(line.strip() for line in str(text or "").replace(
            "\r\n", "\n").replace("\r", "\n").split("\n") if line.strip())
        if not body or int(length) <= 0:
            continue
        n += 1
        t0 = int(start) / fps
        t1 = (int(start) + int(length)) / fps
        out.append("%d\n%s --> %s\n%s\n" % (n, _srt_time(t0), _srt_time(t1), body))
    return "\n".join(out), n


def write_wav(path, mono_pcm, stereo):
    # 48 kHz s16le WAV; stereo duplicates the mono mix to both channels
    data = mono_pcm[:len(mono_pcm) // 2 * 2]
    if stereo:
        try:
            import numpy as np
            a = np.frombuffer(data, dtype="<i2")
            data = np.repeat(a, 2).astype("<i2").tobytes()
        except ImportError:
            import array
            a = array.array("h")
            a.frombytes(data)
            b = array.array("h", [0]) * (2 * len(a))
            b[0::2] = a
            b[1::2] = a
            data = b.tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(2 if stereo else 1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(data)
    return path


def build_ffmpeg_cmd(out_path, width, height, fps, encoder, gop, target_mbps,
                     burst_mbps, faststart, audio_wav=None, stereo=True,
                     srt_path=None, language=None, ffmpeg="ffmpeg",
                     pix_fmt_in="rgb24"):
    # One command: raw RGB on stdin (+ WAV) (+ SRT) -> MP4
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", pix_fmt_in,
           "-s", "%dx%d" % (int(width), int(height)),
           "-framerate", fps_rational(fps), "-i", "pipe:0"]
    idx = 1
    a_idx = s_idx = None
    if audio_wav:
        cmd += ["-i", audio_wav]
        a_idx = idx
        idx += 1
    if srt_path:
        cmd += ["-f", "srt", "-i", srt_path]
        s_idx = idx
        idx += 1
    cmd += ["-map", "0:v:0"]
    if a_idx is not None:
        cmd += ["-map", "%d:a:0" % a_idx]
    if s_idx is not None:
        cmd += ["-map", "%d:s:0" % s_idx]
    t, b = mbps_text(target_mbps), mbps_text(burst_mbps)
    if encoder == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-profile:v", "high", "-rc", "vbr",
                "-b:v", t, "-maxrate", b, "-bufsize", b, "-g", str(int(gop))]
    else:
        cmd += ["-c:v", "libx264", "-profile:v", "high",
                "-b:v", t, "-maxrate", b, "-bufsize", b, "-g", str(int(gop))]
    cmd += ["-pix_fmt", "yuv420p"]
    if a_idx is not None:
        cmd += ["-c:a", "aac", "-b:a", AAC_STEREO if stereo else AAC_MONO,
                "-ac", "2" if stereo else "1", "-ar", str(SAMPLE_RATE)]
    if s_idx is not None:
        cmd += ["-c:s", "mov_text"]
        if language:
            cmd += ["-metadata:s:s:0", "language=%s" % language]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", "mp4", out_path]
    return cmd


def pick_encoder(ffmpeg="ffmpeg"):
    # h264_nvenc if listed by -encoders AND a tiny test encode works (a
    # listed NVENC can still fail with no NVIDIA GPU / driver), else
    # libx264. Cached per session. Raises OSError if ffmpeg is missing.
    if "h264" in _encoder_cache:
        return _encoder_cache["h264"]
    proc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_no_window_flags())
    listed = proc.stdout.decode("ascii", "replace")
    enc = "libx264"
    if " h264_nvenc " in listed:
        test = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x144:r=30:d=0.2", "-c:v", "h264_nvenc",
             "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_no_window_flags())
        if test.returncode == 0:
            enc = "h264_nvenc"
    elif " libx264 " not in listed:
        raise OSError("ffmpeg has neither h264_nvenc nor libx264")
    _encoder_cache["h264"] = enc
    return enc


# ---------------------------------------------------------------- DB reads

def render_length_frames(project_db, sequence_id):
    # BUILD 100 duration: latest out-point of clips, captions, keyframe clips
    import sqlite3
    from sequence_manager import sequence_stats
    conn = sqlite3.connect(project_db)
    try:
        st = sequence_stats(conn.cursor()).get(int(sequence_id), {})
    finally:
        conn.close()
    return int(st.get("frames", 0) or 0)


def load_render_captions(project_db, sequence_id):
    # ([(start, length, text)], [languages]) from enabled caption tracks
    import sqlite3
    caps, langs = [], []
    conn = sqlite3.connect(project_db)
    try:
        tracks = conn.execute(
            "SELECT id, COALESCE(language, '') FROM tracks WHERE sequence_id = ? "
            "AND LOWER(track_type) = 'caption' AND COALESCE(enabled, 1) = 1",
            (int(sequence_id),)).fetchall()
        for tid, lang in tracks:
            rows = conn.execute(
                "SELECT start_frame, length_frames, text FROM captions "
                "WHERE sequence_id = ? AND track_id = ?",
                (int(sequence_id), int(tid))).fetchall()
            if rows:
                caps.extend((int(a), int(b), t) for a, b, t in rows)
                langs.append(lang)
    finally:
        conn.close()
    return caps, langs


def mix_mono_pcm(project_db, sequence_id, fps, total_frames):
    # (pcm, has_audio): the preview mix for the whole range, fresh
    from sequence_preview_audio import (_load_mix_rows, _track_order,
                                        _load_volume_clips, _mix_pcm)
    rows = _load_mix_rows(project_db, sequence_id)
    has_audio = any(r.get("length_frames", 0) > 0 and r.get("basename") for r in rows)
    if not has_audio:
        return b"", False
    order = _track_order(project_db, sequence_id)
    vols = _load_volume_clips(project_db, sequence_id, order)
    return _mix_pcm(project_db, rows, fps, total_frames, vols, order), True


# ---------------------------------------------------------------- GPU frames

class _RenderGpu(object):
    # Render-owned decoders and device buffers, swapped into the player
    # only while the render holds _GEN_LOCK, so the preview / play
    # decoders (proxy paths) and the render decoders (originals) never
    # prune each other.

    def __init__(self, player):
        self.player = player
        self.decoders = {}
        self.dec_next = {}
        self.dec_used = {}
        self.bufs = []

    def _acquire(self):
        # Yield to a GUI waiting on the lock, then take it (no idle wait:
        # the render runs at full speed while the user is idle or not)
        from sequence_player_gpu import _GEN_LOCK, _gui_wants
        while _gui_wants[0] > 0:
            time.sleep(0.005)
        _GEN_LOCK.acquire()
        return _GEN_LOCK

    def _swap(self):
        p = self.player
        saved = (p.decoders, p.dec_next, p.dec_used)
        p.decoders, p.dec_next, p.dec_used = self.decoders, self.dec_next, self.dec_used
        return saved

    def _unswap(self, saved):
        p = self.player
        self.decoders, self.dec_next, self.dec_used = p.decoders, p.dec_next, p.dec_used
        p.decoders, p.dec_next, p.dec_used = saved
        p._target = None

    def frames_rgb(self, frames, snapshot):
        # Host RGB24 bytes per frame (black background), one card pass
        import ctypes
        from sequence_player_gpu import _libs, _check, _new_tm
        lock = self._acquire()
        try:
            p = self.player
            if p.closed or not p.ready:
                raise RuntimeError("The Sequence Player was closed during the render.")
            cudart, nvcuda = _libs()[0], _libs()[1]
            saved = self._swap()
            try:
                _check(nvcuda.cuCtxSetCurrent(p.cuda_ctx), "ctx")
                size = p.row * p.seq_h
                while len(self.bufs) < len(frames):
                    ptr = ctypes.c_void_p()
                    _check(cudart.cudaMalloc(ctypes.byref(ptr), size), "render buffer malloc")
                    self.bufs.append(ptr)
                p._tm = _new_tm()
                pre = p._prefetch(frames, snapshot)
                for k, fr in enumerate(frames):
                    p._build_dst(fr, True, self.bufs[k], snapshot, False, pre, sync=False)
                _check(cudart.cudaStreamSynchronize(p.stream), "render sync")
                out = []
                for k in range(len(frames)):
                    host = ctypes.create_string_buffer(size)
                    _check(cudart.cudaMemcpy(host, self.bufs[k], size, 2), "render to host")
                    out.append(host.raw)
                del pre
                return out
            finally:
                self._unswap(saved)
        finally:
            lock.release()

    def close(self):
        # Render decoders and buffers freed (player still holds the context)
        lock = self._acquire()
        try:
            from sequence_player_gpu import _libs
            p = self.player
            try:
                if p.ready:
                    _libs()[1].cuCtxSetCurrent(p.cuda_ctx)
            except Exception:
                pass
            for key in list(self.decoders.keys()):
                self.decoders.pop(key, None)
            self.dec_next.clear()
            self.dec_used.clear()
            try:
                cudart = _libs()[0]
                for b in self.bufs:
                    if b.value:
                        cudart.cudaFree(b)
            except Exception:
                pass
            self.bufs = []
        finally:
            lock.release()


# ---------------------------------------------------------------- render job

class RenderJob(object):
    # Built on the GUI thread (snapshot, captions, sizes), run on a thread.
    # frame_source(frames) -> list of RGB24 bytes; tests pass a CPU one.

    def __init__(self, params, frame_source, on_done):
        self.p = params
        self.frame_source = frame_source
        self.on_done = on_done            # on_done(ok, message), any thread
        self.cancelled = False
        self.proc = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="RenderJob", daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancelled = True
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _run(self):
        try:
            if self.p.get("cuda", False):
                try:
                    from cuda_context import ensure_current
                    ensure_current()
                except Exception as e:
                    # print("Render CUDA attach failed: %s" % e)
                    pass
            ok, msg = self._render()
        except Exception as e:
            ok, msg = False, "Render failed: %s" % e
        try:
            self.on_done(ok, msg)
        except Exception:
            pass

    def _render(self):
        p = self.p
        t_start = time.perf_counter()
        total = int(p["total_frames"])
        if total <= 0:
            return False, "The sequence is empty (length 0 frames)."
        out_path = p["out_path"]
        if not out_path:
            return False, "No output path."
        tmp = tempfile.mkdtemp(prefix="ladd_render_")
        err_path = os.path.join(tmp, "ffmpeg_stderr.txt")
        try:
            # audio (fresh mix of the whole range)
            t0 = time.perf_counter()
            wav = None
            if p.get("mono_pcm") is not None:
                pcm, has_audio = p["mono_pcm"], bool(p["mono_pcm"])
            else:
                pcm, has_audio = mix_mono_pcm(p["project_db"], p["sequence_id"],
                                              p["fps"], total)
            if has_audio:
                wav = write_wav(os.path.join(tmp, "audio.wav"), pcm, p["stereo"])
            t_audio = time.perf_counter() - t0
            # captions
            srt = None
            lang = None
            if p.get("captions_on"):
                text, n = srt_text(p.get("captions", []), p["fps"])
                if n:
                    srt = os.path.join(tmp, "captions.srt")
                    with open(srt, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                    lang = language_tag(p.get("languages", []))
            if self.cancelled:
                return False, "Render cancelled."
            enc = p.get("encoder") or pick_encoder(p.get("ffmpeg", "ffmpeg"))
            cmd = build_ffmpeg_cmd(out_path, p["width"], p["height"], p["fps"],
                                   enc, p["gop"], p["target_mbps"], p["burst_mbps"],
                                   p["faststart"], wav, p["stereo"], srt, lang,
                                   p.get("ffmpeg", "ffmpeg"))
            # print("RENDER cmd: %s" % subprocess.list2cmdline(cmd))
            d = os.path.dirname(out_path)
            if d and not os.path.isdir(d):
                return False, "Output folder does not exist:\n%s" % d
            frame_bytes = int(p["width"]) * int(p["height"]) * 3
            chunk = max(1, int(p.get("chunk", 8)))
            t_comp = t_pipe = 0.0
            with open(err_path, "wb") as err:
                try:
                    self.proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=err, creationflags=_no_window_flags())
                except OSError as e:
                    return False, "Could not start ffmpeg: %s" % e
                broken = False
                f = 0
                try:
                    while f < total and not self.cancelled:
                        frames = list(range(f, min(total, f + chunk)))
                        c0 = time.perf_counter()
                        data = self.frame_source(frames)
                        c1 = time.perf_counter()
                        t_comp += c1 - c0
                        if len(data) != len(frames):
                            raise RuntimeError("composite returned %d of %d frames"
                                               % (len(data), len(frames)))
                        try:
                            for raw in data:
                                if len(raw) != frame_bytes:
                                    raise RuntimeError("frame size %d, expected %d"
                                                       % (len(raw), frame_bytes))
                                self.proc.stdin.write(raw)
                        except (BrokenPipeError, OSError):
                            broken = True
                            break
                        t_pipe += time.perf_counter() - c1
                        f += len(frames)
                except Exception as e:
                    # Composite failed (player closed, CUDA error): stop ffmpeg
                    try:
                        self.proc.kill()
                        self.proc.wait()
                    except Exception:
                        pass
                    _remove(out_path)
                    return False, "Render failed at frame %d: %s" % (f, e)
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                code = self.proc.wait()
            if self.cancelled:
                _remove(out_path)
                return False, "Render cancelled."
            if code != 0 or broken or not os.path.isfile(out_path):
                _remove(out_path)
                return False, ("ffmpeg failed (exit code %s)\n\nCommand:\n%s\n\n"
                               "stderr (last 15 lines):\n%s"
                               % (code, subprocess.list2cmdline(cmd), _tail(err_path)))
            el = time.perf_counter() - t_start
            print("WORKER render: %d frames %.1fs (%.1f fps) composite %.1f ms/f "
                  "pipe %.1f ms/f audio %.2fs encoder %s"
                  % (total, el, total / el if el > 0 else 0.0,
                     1000.0 * t_comp / total, 1000.0 * t_pipe / total, t_audio, enc))
            parts = ["%d frames" % total, enc,
                     ("stereo" if p["stereo"] else "mono") if wav else "no audio"]
            if srt:
                parts.append("captions" + (" (%s)" % lang if lang else ""))
            return True, "Rendered %s\n%s, %.1f s" % (out_path, ", ".join(parts), el)
        finally:
            self.proc = None
            shutil.rmtree(tmp, ignore_errors=True)


def _tail(path, n=15):
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-n:])


def _remove(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------- window

def _make_window_class():
    from PySide6.QtCore import QObject, Qt, QTimer, Signal
    from PySide6.QtWidgets import (
        QMainWindow, QWidget, QFormLayout, QVBoxLayout, QHBoxLayout,
        QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QRadioButton,
        QButtonGroup, QPushButton, QLabel,
    )

    class _Bridge(QObject):
        done = Signal(bool, str)

    class RenderManagerWindow(QMainWindow):
        def __init__(self, timeline):
            super().__init__()
            self.timeline = timeline
            self.sequence_id = int(timeline.sequence_id)
            self.job = None
            self._closing = False
            self._bridge = _Bridge()
            self._bridge.done.connect(self._on_done, Qt.QueuedConnection)
            from sequence_timeline_sql import _load_sequence_name
            self.seq_name = _load_sequence_name(self.sequence_id) or (
                "Sequence %d" % self.sequence_id)
            self.setWindowTitle("Render Manager: %s: #%d" % (self.seq_name, self.sequence_id))
            central = QWidget()
            outer = QVBoxLayout(central)
            form = QFormLayout()
            self.out_edit = QLineEdit(default_output_name(self.seq_name))
            self.out_edit.setToolTip("A bare name or relative path is saved in the "
                                     "project folder; an absolute path is used as is.")
            form.addRow("Output", self.out_edit)
            self.gop_spin = QSpinBox()
            self.gop_spin.setRange(1, 10000)
            self.gop_spin.setValue(DEFAULT_GOP)
            form.addRow("GOP", self.gop_spin)
            self.target_spin = QDoubleSpinBox()
            self.target_spin.setRange(0.1, 1000.0)
            self.target_spin.setDecimals(1)
            self.target_spin.setValue(DEFAULT_TARGET_MBPS)
            form.addRow("Target Mbps", self.target_spin)
            self.burst_spin = QDoubleSpinBox()
            self.burst_spin.setRange(0.1, 1000.0)
            self.burst_spin.setDecimals(1)
            self.burst_spin.setValue(DEFAULT_BURST_MBPS)
            form.addRow("Burst Mbps", self.burst_spin)
            self.fast_check = QCheckBox("Fast start (YouTube)")
            form.addRow("", self.fast_check)
            audio_row = QHBoxLayout()
            self.mono_radio = QRadioButton("Mono")
            self.stereo_radio = QRadioButton("Stereo")
            self.stereo_radio.setChecked(True)
            self._audio_group = QButtonGroup(self)
            self._audio_group.addButton(self.mono_radio)
            self._audio_group.addButton(self.stereo_radio)
            audio_row.addWidget(self.mono_radio)
            audio_row.addWidget(self.stereo_radio)
            audio_row.addStretch(1)
            form.addRow("Audio", audio_row)
            self.caps_check = QCheckBox("Include captions")
            form.addRow("", self.caps_check)
            outer.addLayout(form)
            row = QHBoxLayout()
            row.addStretch(1)
            self.render_btn = QPushButton("Render")
            self.render_btn.clicked.connect(self._render)
            row.addWidget(self.render_btn)
            outer.addLayout(row)
            self.status = QLabel("")
            self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.status.setWordWrap(True)
            outer.addWidget(self.status)
            outer.addStretch(1)
            self.setCentralWidget(central)
            self.resize(520, 300)

        # ---- render start (GUI thread)

        def _set_busy(self, busy, text=""):
            self.render_btn.setEnabled(not busy)
            if busy:
                self.setCursor(Qt.WaitCursor)       # this window only
            else:
                self.unsetCursor()
            self.status.setText(text)

        def _render(self):
            if self.job is not None:
                return
            from sequence_player import open_player_for
            tl = self.timeline
            self._set_busy(True, "Waiting for the Sequence Player...")
            try:
                player = getattr(tl, "_player", None)
                if player is None or player.closed or not player.ready:
                    player = open_player_for(tl)
            except Exception as e:
                self._fail("Could not open the Sequence Player: %s" % e)
                return
            self._wait_t0 = time.perf_counter()
            self._wait_player(player)

        def _wait_player(self, player):
            # The composite needs the player's CUDA context and kernel
            # (set up on its first expose)
            if self._closing:
                return
            if player.closed or player.failed:
                self._fail("The Sequence Player could not start its GPU setup.")
                return
            if not player.ready:
                if time.perf_counter() - self._wait_t0 > PLAYER_WAIT_S:
                    self._fail("Timed out waiting for the Sequence Player GPU setup.")
                    return
                QTimer.singleShot(100, lambda: self._wait_player(player))
                return
            self._start(player)

        def _start(self, player):
            from sequence_player import timeline_snapshot
            tl = self.timeline
            try:
                project_db = tl._project_db()
                total = render_length_frames(project_db, self.sequence_id)
                caps, langs = ([], [])
                if self.caps_check.isChecked():
                    caps, langs = load_render_captions(project_db, self.sequence_id)
                snapshot = timeline_snapshot(tl, use_proxies=False)
                try:
                    from sequence_preview_worker import _setting_int
                    chunk = _setting_int("previewGpuBatchFrames", 8, 1, 30)
                except Exception:
                    chunk = 8
                params = {
                    "project_db": project_db, "sequence_id": self.sequence_id,
                    "out_path": resolve_output_path(self.out_edit.text(), project_db),
                    "width": player.seq_w, "height": player.seq_h, "fps": tl.fps,
                    "total_frames": total, "gop": self.gop_spin.value(),
                    "target_mbps": self.target_spin.value(),
                    "burst_mbps": self.burst_spin.value(),
                    "faststart": self.fast_check.isChecked(),
                    "stereo": self.stereo_radio.isChecked(),
                    "captions_on": self.caps_check.isChecked(),
                    "captions": caps, "languages": langs,
                    "chunk": chunk, "cuda": True,
                }
            except Exception as e:
                self._fail("Could not prepare the render: %s" % e)
                return
            gpu = _RenderGpu(player)

            def source(frames):
                return gpu.frames_rgb(frames, snapshot)

            def done(ok, msg):
                try:
                    gpu.close()
                except Exception:
                    pass
                self._bridge.done.emit(bool(ok), str(msg))

            self.job = RenderJob(params, source, done)
            self._set_busy(True, "Rendering %d frames to %s ..." % (total, params["out_path"]))
            self.job.start()

        def _fail(self, msg):
            self.job = None
            self._set_busy(False, "")
            from proxy_manager import show_message
            show_message(self, "Render Manager", msg)

        # ---- render end (GUI thread, queued from the render thread)

        def _on_done(self, ok, msg):
            self.job = None
            if self._closing:
                return
            if ok:
                self._set_busy(False, msg)
            else:
                self._set_busy(False, "Render failed.")
                from proxy_manager import show_message
                show_message(self, "Render Manager", msg)

        def closeEvent(self, event):
            # Closing (or New Project) cancels a running render
            self._closing = True
            if self.job is not None:
                self.job.cancel()
            if _open_windows.get(self.sequence_id) is self:
                del _open_windows[self.sequence_id]
            event.accept()

    return RenderManagerWindow


_window_class = []


def open_render_manager(timeline):
    # One window per sequence; raise it if already open
    sid = int(timeline.sequence_id)
    win = _open_windows.get(sid)
    if win is None:
        if not _window_class:
            _window_class.append(_make_window_class())
        win = _window_class[0](timeline)
        _open_windows[sid] = win
    win.show()
    win.raise_()
    win.activateWindow()
    return win


def close_all_render_managers():
    # New Project: close every Render Manager (BUILD 83 pattern)
    for win in list(_open_windows.values()):
        try:
            win.close()
        except RuntimeError:
            pass
    _open_windows.clear()

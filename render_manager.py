# render_manager.py
# BUILD 116: Render Manager, first version. MP4 only (H.264 High + AAC,
# optional mov_text captions). Whole sequence, no progress bar, no in/out.
# BUILD 319: progress bar in the window (frame N of M, busy while the
# preview pauses and the audio mixes; RenderJob.on_progress, throttled).
# BUILD 117: Codec choice H.264 (h264_nvenc / libx264, High) or H.265
# (hevc_nvenc / libx265, Main, -tag:v hvc1); default output is a full path.
# BUILD 344: that default is {ProjectName}_{SequenceName}_vNN.mp4 (the
# sequence name, not the sequence id). NN starts at 01 and is one higher
# than any vNN already in the folder for that name. 'Export subtitles as
# .srt' writes the same stem as an .srt beside the mp4, from enabled
# caption tracks. No cues: no .srt, and the render still succeeds.
# One window per sequence, opened from the Sequence Timeline 'Render' menu.
# Video: the preview composite (timeline_snapshot(tl, use_proxies=False),
# same CUDA blend_layer, scale / position / opacity keyframes) run on the
# open Sequence Player's CUDA context from a render thread, raw RGB frames
# (BUILD 142: on the render's own headless composer, sequence_preview_gpu,
# through the preview's batch loop composite_batch; no player window)
# copied to the host and piped to ffmpeg (rawvideo rgb24); ffmpeg converts
# to yuv420p. Captions are not burned in.
# Audio: the preview mix code (audio tracks, gain_db, volume keyframes)
# rendered fresh into a temp WAV, mono or stereo (duplicated).
# BUILD 120: 'MKV: Use 100% deinterlaced proxies' (auto-checked when the
# sequence has an MKV clip with a 100% deinterlace / IVTC proxy file):
# those clips decode that proxy (timeline_snapshot(tl, use_proxies=False,
# render_deint=True)); every other clip and percent stays the original.
# Scale math stays based on the original size.
# BUILD 318: H.264 over 4096 pixels wide or high never uses h264_nvenc
# (the NVENC H.264 hardware limit): that render uses hevc_nvenc when its
# test encode works, else libx264, with a non-modal warning at the start
# (encoder_for); the codec choice itself is unchanged. While a render
# runs the preview worker renders nothing (sequence_preview_worker
# set_export_pause): Render waits for the worker's current pass to stop
# before the render's GPU composer is created, and every end of the
# render (done, failed, cancelled, window closed) resumes the preview.
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
PLAYER_WAIT_S = 15.0        # wait for the Sequence Player GPU setup (unused since BUILD 142)
DEFAULT_GOP = 15
DEFAULT_TARGET_MBPS = 5.0
DEFAULT_BURST_MBPS = 10.0

_open_windows = {}          # sequence_id -> RenderManagerWindow
_nvenc_ok = {}              # BUILD 117: NVENC encoder name -> test encode ok
# BUILD 117: codec choice -> (NVENC encoder, CPU fallback)
CODECS = (("H.264", "h264_nvenc", "libx264"), ("H.265", "hevc_nvenc", "libx265"))

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


def _output_sequence_stem(sequence_id, sequence_name):
    # File-name piece for the sequence. A name is kept readable: only
    # characters illegal in a Windows file name are replaced (spaces stay).
    # No name: the sequence id, so a project with no sequence name still
    # gets a unique default (Flave_1_v01.mp4).
    name = (sequence_name or "").strip()
    if not name:
        try:
            return str(int(sequence_id))
        except (TypeError, ValueError):
            return "sequence"
    return _sanitize(name)


def next_output_path(project_db, sequence_id, sequence_name=""):
    # Default render output, in the project file's folder:
    # {ProjectName}_{SequenceName}_vNN.mp4. NN is at least two digits, one
    # higher than the highest vNN already in that folder for this project
    # and sequence name (case-insensitive; a missing number is not reused,
    # so v01 plus v03 yields v04). Shown with forward slashes
    # (P:/Flave/Flave_My Seq_v02.mp4); Windows and ffmpeg accept them.
    # No project path: bare '<sequence name>_v01.mp4'.
    if not project_db:
        return "%s_v01.mp4" % _sanitize(sequence_name)
    import re
    from sequence_preview import project_name
    folder = os.path.dirname(os.path.abspath(project_db))
    stem = _output_sequence_stem(sequence_id, sequence_name)
    base = "%s_%s" % (project_name(project_db), stem)
    pat = re.compile(r"^%s_v(\d+)\.mp4$" % re.escape(base), re.IGNORECASE)
    top = 0
    try:
        for f in os.listdir(folder):
            m = pat.match(f)
            if m:
                top = max(top, int(m.group(1)))
    except OSError:
        pass
    return os.path.join(folder, "%s_v%02d.mp4" % (base, top + 1)).replace("\\", "/")


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


def srt_sidecar_path(out_path):
    # Flave_MySeq_v01.mp4 -> Flave_MySeq_v01.srt, same folder. A video path
    # that already ends in .srt gets an extra suffix so the subtitle file
    # does not replace the rendered video.
    path = os.path.splitext(out_path)[0] + ".srt"
    if os.path.normcase(os.path.normpath(path)) == os.path.normcase(
            os.path.normpath(out_path)):
        path = out_path + ".srt"
    return path


def write_srt_export(out_path, captions, fps, enabled=True):
    # Export-subtitles checkbox. Off: write nothing. On, with cues: write
    # the sidecar and return (path, cue count). On, with no cues: write
    # nothing and return (None, 0); the render still succeeds. OSError
    # propagates so the caller can keep the video.
    if not enabled:
        return None, 0
    text, n = srt_text(captions, fps)
    if n <= 0:
        return None, 0
    path = srt_sidecar_path(out_path)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path, n


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
    elif encoder == "hevc_nvenc":
        cmd += ["-c:v", "hevc_nvenc", "-profile:v", "main", "-rc", "vbr",
                "-b:v", t, "-maxrate", b, "-bufsize", b, "-g", str(int(gop)),
                "-tag:v", "hvc1"]
    elif encoder == "libx265":
        cmd += ["-c:v", "libx265", "-profile:v", "main",
                "-b:v", t, "-maxrate", b, "-bufsize", b, "-g", str(int(gop)),
                "-tag:v", "hvc1"]
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


def codec_encoders(codec):
    # (nvenc, fallback) for 'H.264' / 'H.265'; H.264 if unknown
    for name, nv, cpu in CODECS:
        if name == codec:
            return nv, cpu
    return CODECS[0][1], CODECS[0][2]


def nvenc_works(encoder, ffmpeg="ffmpeg", listed=None):
    # Listed by -encoders AND a tiny test encode works (a listed NVENC can
    # still fail with no NVIDIA GPU / driver). Cached per encoder.
    if encoder in _nvenc_ok:
        return _nvenc_ok[encoder]
    if listed is None:
        listed = _list_encoders(ffmpeg)
    ok = False
    if (" %s " % encoder) in listed:
        test = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x144:r=30:d=0.2", "-c:v", encoder,
             "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_no_window_flags())
        ok = test.returncode == 0
    _nvenc_ok[encoder] = ok
    return ok


def _list_encoders(ffmpeg="ffmpeg"):
    proc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_no_window_flags())
    return proc.stdout.decode("ascii", "replace")


def pick_encoder(codec="H.264", ffmpeg="ffmpeg"):
    # NVENC encoder of the codec if its test encode works, else the CPU
    # encoder (libx264 / libx265). Raises OSError if ffmpeg is missing or
    # has neither.
    nv, cpu = codec_encoders(codec)
    listed = _list_encoders(ffmpeg)
    if nvenc_works(nv, ffmpeg, listed):
        return nv
    if (" %s " % cpu) not in listed:
        raise OSError("ffmpeg has neither %s nor %s" % (nv, cpu))
    return cpu


# BUILD 318: largest width / height the NVENC H.264 encoder takes (all
# NVIDIA GPUs); H.265 NVENC goes to 8192 on Pascal and newer
H264_NVENC_MAX = 4096
H264_LIMIT_TEXT = ("H.264 hardware encoding is limited to %d pixels. "
                   "This %s export will use %s.")


def encoder_for(codec, width, height, ffmpeg="ffmpeg"):
    # BUILD 318: (encoder, warning text or "") for a render of width x
    # height. pick_encoder, except that h264_nvenc over H264_NVENC_MAX
    # in either direction becomes hevc_nvenc (its test encode works) or
    # libx264, with the warning text for the user.
    enc = pick_encoder(codec, ffmpeg)
    if enc != "h264_nvenc" or (int(width) <= H264_NVENC_MAX
                               and int(height) <= H264_NVENC_MAX):
        return enc, ""
    size = "%dx%d" % (int(width), int(height))
    if nvenc_works("hevc_nvenc", ffmpeg):
        return "hevc_nvenc", H264_LIMIT_TEXT % (H264_NVENC_MAX, size,
                                                "H.265 (NVENC) instead")
    return "libx264", H264_LIMIT_TEXT % (H264_NVENC_MAX, size,
                                         "CPU H.264 (slower)")


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


def sequence_deint_proxies(project_db, sequence_id):
    # {clip_id: proxy} of the sequence's MKV clips that have a 100%
    # deinterlaced (or IVTC) proxy file (the render override's choice)
    try:
        import sqlite3
        conn = sqlite3.connect(project_db)
        try:
            tids = [r[0] for r in conn.execute(
                "SELECT id FROM tracks WHERE sequence_id = ?", (int(sequence_id),))]
        finally:
            conn.close()
        from sequence_player import resolve_proxies
        return resolve_proxies(project_db, tids, use_proxies=False, render_deint=True)
    except Exception:
        return {}


def mix_mono_pcm(project_db, sequence_id, fps, total_frames):
    # (pcm bytes, has_audio): the preview mix for the whole range, fresh
    # (sequence_preview_audio.mix_full_pcm: the preview's full mix path)
    from sequence_preview_audio import (_load_mix_rows, _track_order,
                                        _load_volume_clips, mix_full_pcm)
    rows = _load_mix_rows(project_db, sequence_id)
    has_audio = any(r.get("length_frames", 0) > 0 and r.get("basename") for r in rows)
    if not has_audio:
        return b"", False
    order = _track_order(project_db, sequence_id)
    vols = _load_volume_clips(project_db, sequence_id, order)
    return mix_full_pcm(project_db, rows, fps, total_frames, vols, order), True


# ---------------------------------------------------------------- GPU frames

class _RenderGpu(object):
    # BUILD 142: the render's own headless GPU composer
    # (sequence_preview_gpu.new_composer: shared CUDA context, own decoders,
    # images and device buffers; no Sequence Player, no OpenGL). Frames go
    # through the same batch loop as the preview fill
    # (sequence_player_preview.composite_batch) with the host RGB sink.
    # Was (BUILD 116-141): render decoders swapped into the open player.

    def __init__(self, composer):
        self.comp = composer
        self.bufs = []

    def _acquire(self):
        # Yield to a GUI waiting on the lock, then take it (no idle wait:
        # the render runs at full speed while the user is idle or not)
        from sequence_player_gpu import _GEN_LOCK, _gui_wants
        while _gui_wants[0] > 0:
            time.sleep(0.005)
        _GEN_LOCK.acquire()
        return _GEN_LOCK

    def frames_rgb(self, frames, snapshot):
        # Host RGB24 bytes per frame (black background), one card pass
        import ctypes
        from sequence_player_gpu import _libs, _check
        from sequence_player_preview import composite_batch, host_rgb
        lock = self._acquire()
        try:
            p = self.comp
            if p.closed or not p.ready:
                raise RuntimeError("The render GPU compositor was released.")
            cudart = _libs()[0]
            _check(_libs()[1].cuCtxSetCurrent(p.cuda_ctx), "ctx")
            size = p.row * p.seq_h
            while len(self.bufs) < len(frames):
                ptr = ctypes.c_void_p()
                _check(cudart.cudaMalloc(ctypes.byref(ptr), size), "render buffer malloc")
                self.bufs.append(ptr)
            try:
                return composite_batch(p, frames, snapshot, True,
                                       self.bufs[:len(frames)], host_rgb,
                                       sync_name="render sync")
            finally:
                p._target = None
        finally:
            lock.release()

    def close(self):
        # Render buffers and the render's composer (decoders) freed; the
        # shared context stays
        lock = self._acquire()
        try:
            from sequence_player_gpu import _libs
            try:
                if self.comp.cuda_ctx.value:
                    _libs()[1].cuCtxSetCurrent(self.comp.cuda_ctx)
            except Exception:
                pass
            try:
                cudart = _libs()[0]
                for b in self.bufs:
                    if b.value:
                        cudart.cudaFree(b)
            except Exception:
                pass
            self.bufs = []
            from sequence_preview_gpu import free_composer
            free_composer(self.comp)
        finally:
            lock.release()


# ---------------------------------------------------------------- render job

# BUILD 319: render progress. RenderJob.on_progress(done, total) from the
# render thread: PROGRESS_BUSY while the audio mixes, then frames written
# (0 .. total), at most one call per PROGRESS_MIN_S (first / last always)
PROGRESS_BUSY = -1
PROGRESS_IDLE = -2
PROGRESS_MIN_S = 0.1


def _set_progress_bar(bar, done, total):
    # BUILD 319: QProgressBar state (GUI thread): busy (indeterminate),
    # idle (empty) or frame done of total with the percent
    if bar is None:
        return
    done, total = int(done), int(total)
    if done == PROGRESS_BUSY:
        bar.setRange(0, 0)
        bar.setFormat("")
    elif done < 0 or total <= 0:
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFormat("")
    else:
        done = min(done, total)
        bar.setRange(0, total)
        bar.setValue(done)
        bar.setFormat("%%p%%  (frame %d of %d)" % (done, total))


class RenderJob(object):
    # Built on the GUI thread (snapshot, captions, sizes), run on a thread.
    # frame_source(frames) -> list of RGB24 bytes; tests pass a CPU one.

    def __init__(self, params, frame_source, on_done):
        self.p = params
        self.frame_source = frame_source
        self.on_done = on_done            # on_done(ok, message), any thread
        self.on_warn = None               # BUILD 318: on_warn(text), any thread
        self.on_progress = None           # BUILD 319: on_progress(done, total)
        self._progress_t = None
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

    def _progress(self, done, total, force=False):
        # BUILD 319: throttled on_progress (render thread)
        cb = self.on_progress
        if cb is None:
            return
        now = time.perf_counter()
        if (not force and self._progress_t is not None
                and now - self._progress_t < PROGRESS_MIN_S):
            return
        self._progress_t = now
        try:
            cb(int(done), int(total))
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
        # BUILD 223: system temp folder {ProjectName}_{sequence_id}_Render_<random>
        # holding {ProjectName}_{sequence_id}_RenderAudio.wav, _RenderCaptions.srt
        # and _RenderStderr.txt; removed after the render
        _base = "%s_%d" % (os.path.splitext(os.path.basename(
            p.get("project_db") or "FLAVE"))[0], int(p.get("sequence_id") or 0))
        tmp = tempfile.mkdtemp(prefix=_base + "_Render_")
        err_path = os.path.join(tmp, _base + "_RenderStderr.txt")
        try:
            # audio (fresh mix of the whole range)
            self._progress(PROGRESS_BUSY, total, True)  # BUILD 319: no audio progress
            t0 = time.perf_counter()
            wav = None
            if p.get("mono_pcm") is not None:
                pcm, has_audio = p["mono_pcm"], bool(p["mono_pcm"])
            else:
                pcm, has_audio = mix_mono_pcm(p["project_db"], p["sequence_id"],
                                              p["fps"], total)
            if has_audio:
                wav = write_wav(os.path.join(tmp, _base + "_RenderAudio.wav"), pcm, p["stereo"])
            t_audio = time.perf_counter() - t0
            # captions
            srt = None
            lang = None
            if p.get("captions_on"):
                text, n = srt_text(p.get("captions", []), p["fps"])
                if n:
                    srt = os.path.join(tmp, _base + "_RenderCaptions.srt")
                    with open(srt, "w", encoding="utf-8", newline="\n") as f:
                        f.write(text)
                    lang = language_tag(p.get("languages", []))
            if self.cancelled:
                return False, "Render cancelled."
            enc = p.get("encoder")
            if not enc:
                # BUILD 318: H.264 over 4096 falls back (encoder_for)
                enc, warn = encoder_for(p.get("codec", "H.264"), p["width"],
                                        p["height"], p.get("ffmpeg", "ffmpeg"))
                if warn and self.on_warn is not None:
                    try:
                        self.on_warn(warn)
                    except Exception:
                        pass
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
                self._progress(0, total, True)      # BUILD 319
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
                        self._progress(f, total, f >= total)  # BUILD 319
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
            # BUILD 312: the render timing summary prints only with
            # RENDER_LOG (the result message still names frames and time)
            import preview_log
            if preview_log.RENDER_LOG:
                print("WORKER render: %d frames %.1fs (%.1f fps) composite %.1f ms/f "
                      "pipe %.1f ms/f audio %.2fs encoder %s"
                      % (total, el, total / el if el > 0 else 0.0,
                         1000.0 * t_comp / total, 1000.0 * t_pipe / total,
                         t_audio, enc))
            parts = ["%d frames" % total, enc,
                     ("stereo" if p["stereo"] else "mono") if wav else "no audio"]
            if srt:
                parts.append("captions" + (" (%s)" % lang if lang else ""))
            # BUILD 344: sidecar .srt only after the mp4 exists, and only
            # when the checkbox is on. No cues, or a write error, leaves
            # the finished video in place.
            if p.get("export_srt"):
                try:
                    side, _n = write_srt_export(
                        out_path, p.get("captions") or [], p["fps"], enabled=True)
                except OSError:
                    parts.append("srt not written")
                else:
                    parts.append("srt" if side else "no srt cues")
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
        QButtonGroup, QPushButton, QLabel, QComboBox, QProgressBar,
    )

    class _Bridge(QObject):
        done = Signal(bool, str)
        warn = Signal(str)      # BUILD 318: encoder fallback warning
        progress = Signal(int, int)     # BUILD 319: frames done, total

    class RenderManagerWindow(QMainWindow):
        def __init__(self, timeline):
            super().__init__()
            self.timeline = timeline
            self.sequence_id = int(timeline.sequence_id)
            self.job = None
            self._closing = False
            self._user_cancel = False
            self._bridge = _Bridge()
            self._bridge.done.connect(self._on_done, Qt.QueuedConnection)
            self._bridge.warn.connect(self._on_warn, Qt.QueuedConnection)
            self._bridge.progress.connect(self._on_progress, Qt.QueuedConnection)
            self._render_total = 0
            # BUILD 318: [True] while this window holds the preview export
            # pause (set_export_pause); _unpause clears it exactly once
            self._pause = None
            self._pause_wait = False
            from sequence_timeline_sql import _load_sequence_name
            self.seq_name = _load_sequence_name(self.sequence_id) or (
                "Sequence %d" % self.sequence_id)
            self.setWindowTitle("Render Manager: %s: #%d" % (self.seq_name, self.sequence_id))
            central = QWidget()
            outer = QVBoxLayout(central)
            form = QFormLayout()
            try:
                project_db = timeline._project_db()
            except Exception:
                project_db = ""
            # BUILD 344: next free {ProjectName}_{SequenceName}_vNN.mp4;
            # refilled by refresh_output_path (window shown again, render
            # finished). A path the user typed is replaced only then, same
            # as before.
            self.out_edit = QLineEdit(
                next_output_path(project_db, self.sequence_id, self.seq_name))
            self.out_edit.setToolTip("A bare name or relative path is saved in the "
                                     "project folder; an absolute path is used as is.")
            form.addRow("Output", self.out_edit)
            self.codec_combo = QComboBox()
            for name, _nv, _cpu in CODECS:
                self.codec_combo.addItem(name)
            self.codec_combo.setCurrentIndex(0)
            form.addRow("Codec", self.codec_combo)
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
            self.srt_check = QCheckBox("Export subtitles as .srt")
            self.srt_check.setToolTip(
                "Write an .srt next to the video, same name, from this "
                "sequence's caption clips. Skipped when there are no captions.")
            form.addRow("", self.srt_check)
            self.deint_check = QCheckBox("MKV: Use 100% deinterlaced proxies")
            self.deint_check.setToolTip(
                "MKV clips with a 100% deinterlaced (or inverse telecine) proxy "
                "render from that proxy; all other clips use the originals.")
            self.deint_check.setChecked(
                bool(sequence_deint_proxies(project_db, self.sequence_id)))
            form.addRow("", self.deint_check)
            outer.addLayout(form)
            row = QHBoxLayout()
            row.addStretch(1)
            # BUILD 143: Cancel cancels a running render, else closes the window
            # BUILD 144: Cancel sits left of Render
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.clicked.connect(self._cancel)
            row.addWidget(self.cancel_btn)
            self.render_btn = QPushButton("Render")
            self.render_btn.clicked.connect(self._render)
            row.addWidget(self.render_btn)
            outer.addLayout(row)
            self.status = QLabel("")
            self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.status.setWordWrap(True)
            outer.addWidget(self.status)
            # BUILD 319: render progress (frame N of M)
            self.progress_bar = QProgressBar()
            self.progress_bar.setTextVisible(True)
            _set_progress_bar(self.progress_bar, PROGRESS_IDLE, 0)
            outer.addWidget(self.progress_bar)
            outer.addStretch(1)
            self.setCentralWidget(central)
            self.resize(520, 360)

        # ---- render start (GUI thread)

        def refresh_output_path(self):
            # Put the next free version in the Output field (replaces any
            # typed path); not while a render runs. Same times as before:
            # the window is shown again, or a render finishes.
            if self.job is not None:
                return
            try:
                project_db = self.timeline._project_db()
            except Exception:
                project_db = ""
            self.out_edit.setText(
                next_output_path(project_db, self.sequence_id, self.seq_name))

        def _set_busy(self, busy, text=""):
            self.render_btn.setEnabled(not busy)
            self.render_btn.setText("Rendering" if busy else "Render")
            if busy:
                self.setCursor(Qt.WaitCursor)       # this window only
            else:
                self.unsetCursor()
            self.status.setText(text)
            # BUILD 319: busy bar while busy (until frames report), else empty
            _set_progress_bar(getattr(self, "progress_bar", None),
                              PROGRESS_BUSY if busy else PROGRESS_IDLE, 0)

        def _unpause(self, token):
            # BUILD 318: release this render's preview export pause once
            # (any thread); the last release wakes the preview worker
            if token is not None and token[0]:
                token[0] = False
                try:
                    from sequence_preview_worker import set_export_pause
                    set_export_pause(False)
                except Exception as e:
                    print(f"Preview resume failed: {e}")

        def _render(self):
            if self.job is not None or self._pause_wait:
                return
            self._user_cancel = False
            # BUILD 318: the preview worker stops rendering for the whole
            # render; the GPU composer is created once its pass has stopped
            try:
                from sequence_preview_worker import set_export_pause
                set_export_pause(True)
                self._pause = [True]
            except Exception as e:
                print(f"Preview pause failed: {e}")
                self._pause = None
            self._pause_wait = True
            self._set_busy(True, "Pausing preview rendering...")
            self._render_when_paused()

        def _render_when_paused(self):
            # BUILD 318: poll (50 ms, GUI thread) until the preview worker
            # is out of its pass, then start; Cancel / close stop the wait
            if not self._pause_wait:
                return
            if self._closing:
                self._pause_wait = False
                self._unpause(self._pause)
                self._pause = None
                return
            try:
                from sequence_preview_worker import export_pause_ready
                ready = export_pause_ready()
            except Exception:
                ready = True
            if not ready:
                QTimer.singleShot(50, self._render_when_paused)
                return
            self._pause_wait = False
            self._render_start()

        def _render_start(self):
            # BUILD 142: no Sequence Player and no OpenGL wait: the render
            # composites on its own headless GPU composer
            # from sequence_player import open_player_for
            self._set_busy(True, "Starting the GPU compositor...")
            try:
                from sequence_preview_gpu import new_composer
                comp = new_composer(self.timeline._project_db(), self.sequence_id)
            except Exception as e:
                self._fail("Could not start the GPU compositor: %s" % e)
                return
            self._start(comp)

        def _start(self, player):
            # player: the render's headless composer (BUILD 142; seq_w /
            # seq_h, freed by _RenderGpu.close)
            from sequence_player import timeline_snapshot
            tl = self.timeline
            try:
                project_db = tl._project_db()
                total = render_length_frames(project_db, self.sequence_id)
                caps, langs = ([], [])
                export_srt = self.srt_check.isChecked()
                if self.caps_check.isChecked() or export_srt:
                    caps, langs = load_render_captions(project_db, self.sequence_id)
                snapshot = timeline_snapshot(
                    tl, use_proxies=False, render_deint=self.deint_check.isChecked())
                try:
                    # the distinct decoder keys (file + instance) the
                    # render decodes, on the console
                    import preview_log
                    from sequence_player_preview import decoder_keys_text
                    if preview_log.RENDER_LOG:
                        print("RENDER decoders: " + decoder_keys_text(player, snapshot))
                except Exception as e:
                    print("RENDER decoders: not listed (%s)" % e)
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
                    "codec": self.codec_combo.currentText(),
                    "target_mbps": self.target_spin.value(),
                    "burst_mbps": self.burst_spin.value(),
                    "faststart": self.fast_check.isChecked(),
                    "stereo": self.stereo_radio.isChecked(),
                    "captions_on": self.caps_check.isChecked(),
                    "export_srt": export_srt,
                    "captions": caps, "languages": langs,
                    "chunk": chunk, "cuda": True,
                }
            except Exception as e:
                try:
                    from sequence_preview_gpu import free_composer
                    free_composer(player)
                except Exception:
                    pass
                self._fail("Could not prepare the render: %s" % e)
                return
            gpu = _RenderGpu(player)
            token = self._pause     # BUILD 318: released by done() (or _fail)

            def source(frames):
                return gpu.frames_rgb(frames, snapshot)

            def done(ok, msg):
                try:
                    gpu.close()
                except Exception:
                    pass
                # BUILD 318: render GPU memory freed: the preview resumes
                self._unpause(token)
                self._bridge.done.emit(bool(ok), str(msg))

            self.job = RenderJob(params, source, done)
            self.job.on_warn = self._bridge.warn.emit
            self._render_total = total
            if hasattr(self._bridge, "progress"):
                self.job.on_progress = self._bridge.progress.emit  # BUILD 319
            self._set_busy(True, "Rendering %d frames to %s ..." % (total, params["out_path"]))
            self.job.start()
            self._pause = None      # BUILD 318: done() owns the release now

        def _fail(self, msg):
            self.job = None
            self._unpause(self._pause)      # BUILD 318
            self._pause = None
            self._set_busy(False, "")
            from proxy_manager import show_message
            show_message(self, "Render Manager", msg)

        def _on_warn(self, text):
            # BUILD 318: encoder fallback (encoder_for): status line and a
            # non-modal message box; the render goes on
            if self._closing:
                return
            self.status.setText(text + "\n" + self.status.text())
            try:
                from PySide6.QtWidgets import QMessageBox
                box = QMessageBox(QMessageBox.Warning, "Render Manager", text,
                                  QMessageBox.Ok, self)
                box.setWindowModality(Qt.NonModal)
                box.setAttribute(Qt.WA_DeleteOnClose, True)
                box.show()
                self._warn_box = box
            except Exception as e:
                print(f"Render warning failed: {e}")

        def _on_progress(self, done, total):
            # BUILD 319: queued from the render thread (throttled there)
            if self._closing or self.job is None:
                return
            _set_progress_bar(getattr(self, "progress_bar", None), done, total)

        # ---- render end (GUI thread, queued from the render thread)

        def _on_done(self, ok, msg):
            self.job = None
            if self._closing:
                return
            if self._user_cancel and not ok:
                # Cancel button: no error box, the window stays open
                self._user_cancel = False
                self._set_busy(False, "Render cancelled.")
                return
            self._user_cancel = False
            if ok:
                self._set_busy(False, msg)
                total = int(getattr(self, "_render_total", 0) or 0)
                if total > 0:   # BUILD 319: success ends at 100%
                    _set_progress_bar(getattr(self, "progress_bar", None), total, total)
                # the next render suggests the next version
                self.refresh_output_path()
            else:
                self._set_busy(False, "Render failed.")
                from proxy_manager import show_message
                show_message(self, "Render Manager", msg)

        def _cancel(self):
            # BUILD 143: running render -> cancel it (RenderJob.cancel: flag
            # + kill ffmpeg; the thread ends after the current GPU batch and
            # _on_done restores the idle UI); idle -> close the window
            if self.job is not None:
                self._user_cancel = True
                self.job.cancel()
                self.status.setText("Cancelling the render...")
                return
            if self._pause_wait:
                # BUILD 318: still waiting for the preview to pause
                self._pause_wait = False
                self._unpause(self._pause)
                self._pause = None
                self._set_busy(False, "Render cancelled.")
                return
            self.close()

        def closeEvent(self, event):
            # Closing (or New Project) cancels a running render
            self._closing = True
            if self.job is not None:
                self.job.cancel()
            if self._pause_wait:
                # BUILD 318: closed while waiting for the preview to pause
                self._pause_wait = False
                self._unpause(self._pause)
                self._pause = None
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
        # BUILD 229: under the timeline's menu bar (no geometry memory:
        # every new window)
        from window_place import place_under_parent
        place_under_parent(win, timeline, "render_manager")
    else:
        # BUILD 224: opened again: suggest the next free version
        win.refresh_output_path()
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

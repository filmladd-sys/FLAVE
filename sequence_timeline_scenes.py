# sequence_timeline_scenes.py
# Scene Detect (Slice): video clip right-click on the Sequence Timeline.
# FFmpeg's scene score (select='gt(scene,SCENE_THRESHOLD)',showinfo) runs
# on the clip's source range (its decodable proxy when it has one) in a
# worker thread; every detected cut slices the clip and its linked
# partners right away through the normal slice path
# (sequence_timeline_sql.slice_members_db, the database part of Slice /
# slice mode: linked audio, captions and keyframe clips behave the same).
# A new cut lands in whichever piece holds that frame. One undo step for
# the whole run (push_undo before the first slice); Cancel keeps the
# slices made so far. Cuts closer than MIN_SHOT_FRAMES to the previous
# cut or to either clip edge are skipped (flashes, strobes).
#
# Time mapping: clip edit points are sequence frames, which are time at
# the sequence rate (BUILD 273 conform), so the source range starts at
# in_frame / seq_fps seconds. A cut at source time t is the first source
# frame K = round(t * footage_fps); the sequence frame that first shows it
# is the smallest s with floor(s * footage_fps / seq_fps) >= K, i.e.
# ceil(K * seq_fps / footage_fps) (just round(t * seq_fps) when the rates
# match or the footage rate is unknown).
#
# Decode: NVDEC (-hwaccel cuda, scale_cuda to SCENE_WIDTH, hwdownload)
# when proxy_manager.gpu_decodable says the source can take it, else CPU
# decode + scale. A GPU run that fails is retried on the CPU (cuts already
# made are kept and not repeated). Progress window: application-modal
# like Generate Captions. Console lines start with [scenes]. ASCII only.

import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

from PySide6.QtCore import Qt

# ffmpeg scene score (0..1, mean frame difference after motion-free
# comparison). 0.4 is the usual starting point: hard cuts between shots
# typically score 0.5 and up, fast pans and flashes stay mostly below it
SCENE_THRESHOLD = 0.4
# Shortest shot kept, in sequence frames (about half a second at 24-30 fps)
MIN_SHOT_FRAMES = 12
# Width the frames are scaled to before scoring (height keeps the aspect)
SCENE_WIDTH = 320
# Cuts applied on the GUI thread at most this often (ms)
APPLY_INTERVAL_MS = 500
# Console progress line interval (s)
PROGRESS_LOG_S = 30.0

_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")
_OUT_TIME_RE = re.compile(r"^out_time_(?:us|ms)=(\d+)")


def _log(msg):
    try:
        print("[scenes] " + msg)
        sys.stdout.flush()
    except Exception:
        pass


def _hms(seconds):
    s = max(0, int(seconds or 0))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def _mmss(seconds):
    s = max(0, int(seconds or 0))
    return "%02d:%02d" % (s // 60, s % 60)


def parse_showinfo_pts(line):
    # pts_time (seconds) of a showinfo frame line, else None
    if "showinfo" not in line or "pts_time" not in line:
        return None
    m = _PTS_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def cut_sequence_frame(t_rel, clip_start, in_frame, seq_fps, footage_fps=None,
                       speed=100):
    # Sequence frame of a cut found t_rel seconds into the clip's source
    # range (see the module comment); at a clip speed other than 100 the
    # first timeline frame showing that source frame (BUILD 303)
    seq_fps = float(seq_fps or 30.0)
    t = int(in_frame) / seq_fps + max(0.0, float(t_rel))
    from sequence_player import fps_conform_ratio
    if fps_conform_ratio(footage_fps, seq_fps) is None:
        s = int(round(t * seq_fps))
    else:
        f_fps = float(footage_fps)
        k = int(round(t * f_fps))
        s = int(math.ceil(k * seq_fps / f_fps - 1e-6))
    from clip_speed import timeline_offset
    return int(clip_start) + timeline_offset(s - int(in_frame), speed)


class CutFilter(object):
    # Accepts increasing cut frames inside (start, end) at least
    # MIN_SHOT_FRAMES from the previous accepted cut and from both edges
    def __init__(self, start, end, min_shot=MIN_SHOT_FRAMES):
        self.last = int(start)
        self.end = int(end)
        self.min_shot = int(min_shot)

    def accept(self, frame):
        frame = int(frame)
        if frame - self.last < self.min_shot or self.end - frame < self.min_shot:
            return False
        self.last = frame
        return True


def build_cmd(src, ss, dur, w, h, gpu, threshold=SCENE_THRESHOLD):
    sel = "select='gt(scene,%s)',showinfo" % repr(float(threshold))
    if gpu:
        head = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        vf = ("scale_cuda=%d:%d:format=yuv420p,hwdownload,format=yuv420p,%s"
              % (w, h, sel))
    else:
        head = []
        vf = "scale=%d:%d,%s" % (w, h, sel)
    return (["ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info"]
            + head + ["-ss", "%.6f" % ss, "-t", "%.6f" % dur, "-i", src,
                      "-map", "0:V:0", "-an", "-sn", "-dn", "-vf", vf,
                      "-f", "null", "-progress", "pipe:1", "-"])


def _scaled(w, h):
    try:
        w, h = int(w or 0), int(h or 0)
    except (TypeError, ValueError):
        w = h = 0
    if w <= 0 or h <= 0:
        return SCENE_WIDTH, (SCENE_WIDTH * 9 // 16) // 2 * 2
    sw = min(SCENE_WIDTH, w - w % 2)
    sh = max(2, int(round(h * sw / float(w) / 2.0)) * 2)
    return sw, sh


def run_detection(info, on_cut, on_progress, cancel, proc_box):
    # Worker thread. info: src, ss, dur, size, gpu, start, end, in_frame,
    # seq_fps, footage_fps. on_cut(frame) per accepted cut, on_progress
    # (seconds_done, cuts). Returns (cuts, error text or "").
    flt = CutFilter(info["start"], info["end"])
    state = {"cuts": 0, "pos": 0.0}
    w, h = _scaled(*info["size"])
    plans = [True, False] if info["gpu"] else [False]
    err = ""
    t0 = time.monotonic()
    for gpu in plans:
        cmd = build_cmd(info["src"], info["ss"], info["dur"], w, h, gpu)
        _log("ffmpeg (%s): %s" % ("CUDA" if gpu else "CPU",
                                   subprocess.list2cmdline(cmd)))
        flags = 0x08000000 if sys.platform.startswith("win") else 0
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            return state["cuts"], "Could not start ffmpeg: %s" % e
        proc_box[0] = proc
        tail = []
        last_log = [time.monotonic()]

        def read_progress():
            for raw in proc.stdout:
                m = _OUT_TIME_RE.match(raw.decode("ascii", "replace").strip())
                if not m:
                    continue
                v = int(m.group(1))
                # out_time_us is microseconds; old builds' out_time_ms too
                state["pos"] = max(state["pos"], min(info["dur"], v / 1e6))
                on_progress(state["pos"], state["cuts"])
                now = time.monotonic()
                if now - last_log[0] >= PROGRESS_LOG_S:
                    last_log[0] = now
                    el = now - t0
                    _log("progress: %s of %s, %s elapsed, %.1fx realtime, %d cuts" % (
                        _hms(state["pos"]), _hms(info["dur"]), _hms(el),
                        state["pos"] / el if el > 0 else 0.0, state["cuts"]))

        reader = threading.Thread(target=read_progress, daemon=True)
        reader.start()
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            pts = parse_showinfo_pts(line)
            if pts is None:
                if line and "showinfo" not in line:
                    tail.append(line)
                    del tail[:-15]
                continue
            f = cut_sequence_frame(pts, info["start"], info["in_frame"],
                                   info["seq_fps"], info["footage_fps"],
                                   info.get("speed", 100))
            if flt.accept(f):
                state["cuts"] += 1
                on_cut(f)
                on_progress(state["pos"], state["cuts"])
        code = proc.wait()
        reader.join(2.0)
        proc_box[0] = None
        if cancel.is_set():
            return state["cuts"], ""
        if code == 0:
            return state["cuts"], ""
        err = "ffmpeg exit code %s:\n%s" % (code, "\n".join(tail[-8:]))
        _log("ffmpeg %s run failed (exit %s); last lines:" % (
            "CUDA" if gpu else "CPU", code))
        for t in tail[-8:]:
            _log("  " + t)
        if gpu:
            _log("retrying on the CPU (cuts already made are kept)")
    return state["cuts"], err


# ---- GUI side ---------------------------------------------------------

def _clip_info(tl, clip):
    # Source, range and rates for the right-clicked video clip, or None
    db = tl._project_db()
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT c.track_id, c.start_frame, c.length_frames, "
            "COALESCE(c.in_frame, 0), f.original_path, f.fps, f.width, f.height, "
            "LOWER(COALESCE(t.track_type, '')), c.footage_id "
            "FROM clips c JOIN tracks t ON t.id = c.track_id "
            "LEFT JOIN footage f ON f.id = c.footage_id WHERE c.id = ?",
            (int(clip[0]),)).fetchone()
    finally:
        conn.close()
    if row is None or row[8] != "video" or row[9] is None:
        return None
    track_id, start, length, in_f, src, ffps, fw, fh = row[:8]
    if not src or not os.path.isfile(src):
        return {"error": "Source file not found:\n%s" % (src or "")}
    path, proxy_note = src, ""
    try:
        from sequence_player import resolve_proxies
        pr = resolve_proxies(db, [track_id]).get(int(clip[0]))
        if pr and pr[0]:
            path, proxy_note = pr[0], " (proxy)"
    except Exception:
        pass
    size = (fw, fh)
    if path != src:
        try:
            from clip_proxy import probe_video_info
            pi = probe_video_info(path)
            if pi and pi[0] and pi[1]:
                size = (pi[0], pi[1])
        except Exception:
            pass
    gpu = True
    try:
        from proxy_manager import probe_codec, gpu_decodable
        gpu = gpu_decodable(*probe_codec(path))[0]
    except Exception:
        gpu = True
    seq_fps = float(getattr(tl, "fps", None) or 30.0)
    start, length, in_f = int(start), int(length), int(in_f)
    # BUILD 303: the source range a sped clip covers (clip_speed)
    from clip_speed import speed_of, source_span
    speed = speed_of(getattr(tl, "_clip_speeds", None), int(clip[0]))
    return {"track_id": track_id, "start": start, "end": start + length,
            "in_frame": in_f, "src": path, "note": proxy_note,
            "ss": in_f / seq_fps, "dur": source_span(length, speed) / seq_fps,
            "speed": speed, "size": size,
            "gpu": gpu, "seq_fps": seq_fps,
            "footage_fps": float(ffps) if ffps else None,
            "name": os.path.basename(src)}


def _bridge(tl):
    br = getattr(tl, "_scene_bridge", None)
    if br is None:
        from PySide6.QtCore import QObject, Signal

        class _Bridge(QObject):
            cut = Signal(int)
            progress = Signal(float, int)
            done = Signal(int, str)

        br = _Bridge(tl)
        br.cut.connect(lambda f: _on_cut(tl, f), Qt.QueuedConnection)
        br.progress.connect(lambda p, n: _on_progress(tl, p, n), Qt.QueuedConnection)
        br.done.connect(lambda n, e: _on_done(tl, n, e), Qt.QueuedConnection)
        tl._scene_bridge = br
    return br


def scene_detect(tl, clip):
    # Menu entry: start a run on clip (and its linked partners)
    from PySide6.QtWidgets import QMessageBox
    title = "Scene Detect"
    if getattr(tl, "_scene_job", None) is not None:
        return
    try:
        info = _clip_info(tl, clip)
    except Exception as e:
        QMessageBox.warning(tl, title, "Could not read the clip: %s" % e)
        return
    if info is None:
        QMessageBox.information(tl, title, "Scene Detect works on video clips only.")
        return
    if info.get("error"):
        QMessageBox.warning(tl, title, info["error"])
        return
    if info["end"] - info["start"] < 2 * MIN_SHOT_FRAMES:
        QMessageBox.information(tl, title, "The clip is too short to slice.")
        return
    import threading as _th
    cancel = _th.Event()
    job = {"info": info, "cancel": cancel, "proc": [None], "queue": [],
           "applied": 0, "undo": False, "cuts": 0, "pos": 0.0,
           "t0": time.monotonic(), "anchor": int(clip[0])}
    tl._scene_job = job
    import undo
    undo.set_blocked("scenes:%d" % int(tl.sequence_id), True)
    _open_progress(tl, job)
    br = _bridge(tl)
    _log("detect: %s%s, in %d, %d frames at %.3f fps (footage %s fps), "
         "threshold %s, min shot %d frames, %s" % (
             info["name"], info["note"], info["in_frame"],
             info["end"] - info["start"], info["seq_fps"],
             ("%.3f" % info["footage_fps"]) if info["footage_fps"] else "?",
             SCENE_THRESHOLD, MIN_SHOT_FRAMES,
             "CUDA decode" if info["gpu"] else "CPU decode"))

    def emit(sig, *a):
        try:
            sig.emit(*a)
        except RuntimeError:
            pass   # timeline closed

    def work():
        try:
            n, err = run_detection(info, lambda f: emit(br.cut, int(f)),
                                   lambda p, c: emit(br.progress, float(p), int(c)),
                                   cancel, job["proc"])
        except Exception as e:
            n, err = 0, "Scene detection failed: %s" % e
        emit(br.done, int(n), err)

    _th.Thread(target=work, name="SceneDetect", daemon=True).start()


def _open_progress(tl, job):
    # Application-modal like Generate Captions (BUILD 277): blocks every
    # FLAVE window and comes back to the front when FLAVE is activated
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QProgressDialog
    dlg = QProgressDialog("Detecting scenes...", "Cancel", 0, 1000, tl)
    dlg.setWindowTitle("Scene Detect")
    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setMinimumDuration(0)
    dlg.setMinimumWidth(380)
    dlg.canceled.connect(lambda: _cancel(tl))
    dlg.rejected.connect(lambda: _cancel(tl))

    def keep_on_top(*_args):
        try:
            if dlg.isVisible():
                dlg.raise_()
                if QApplication.applicationState() == Qt.ApplicationActive:
                    dlg.activateWindow()
        except RuntimeError:
            pass

    app = QApplication.instance()
    if app is not None:
        app.focusWindowChanged.connect(keep_on_top)
        app.applicationStateChanged.connect(keep_on_top)
        job["raise_fn"] = keep_on_top
    from window_place import place_under_parent
    place_under_parent(dlg, tl, "scene_progress")
    timer = QTimer(dlg)
    timer.setInterval(APPLY_INTERVAL_MS)
    timer.timeout.connect(lambda: _apply_queued(tl))
    timer.start()
    job["dlg"], job["timer"] = dlg, timer
    dlg.show()


def _label(job):
    return "Detecting scenes: %s of %s, %d cuts" % (
        _mmss(job["pos"]), _mmss(job["info"]["dur"]), job["cuts"])


def _on_progress(tl, pos, cuts):
    job = getattr(tl, "_scene_job", None)
    if job is None:
        return
    job["pos"], job["cuts"] = pos, cuts
    dlg = job.get("dlg")
    if dlg is not None and not job["cancel"].is_set():
        dur = job["info"]["dur"] or 1.0
        dlg.setValue(int(1000 * min(1.0, pos / dur)))
        dlg.setLabelText(_label(job))


def _on_cut(tl, frame):
    job = getattr(tl, "_scene_job", None)
    if job is not None:
        job["queue"].append(int(frame))


def _apply_queued(tl):
    # Slice at every queued cut (oldest first), each in the piece that
    # holds it now; one reload between cuts, one full refresh per batch
    job = getattr(tl, "_scene_job", None)
    if job is None or not job["queue"]:
        return
    cuts, job["queue"] = job["queue"], []
    info = job["info"]
    from sequence_timeline_sql import slice_members_db, refresh as reload_clips
    db = tl._project_db()
    done = 0
    for f in cuts:
        piece = None
        for c in tl.clips_by_track.get(info["track_id"], []):
            if (info["start"] <= c[2] < info["end"] and c[2] < f < c[2] + c[3]):
                piece = c
                break
        if piece is None:
            continue
        if not job["undo"]:
            from undo import push_undo
            push_undo(db, "Scene Detect")
            job["undo"] = True
        linked = len(piece) > 8 and piece[8] is not None
        members = [piece] + tl._linked_clips(piece)
        try:
            conn = sqlite3.connect(db)
            try:
                slice_members_db(tl, conn.cursor(), members, int(f), linked)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            _log("slice at frame %d failed: %s" % (f, e))
            continue
        done += 1
        reload_clips(tl)
    if done:
        job["applied"] += done
        tl._after_clip_change("Scene Detect: %d cuts" % job["applied"])


def _cancel(tl):
    job = getattr(tl, "_scene_job", None)
    if job is None or job["cancel"].is_set():
        return
    job["cancel"].set()
    proc = job["proc"][0]
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    _log("cancelled by the user")
    dlg = job.get("dlg")
    if dlg is not None:
        dlg.setLabelText("Cancelling...")


def _on_done(tl, cuts, err):
    job = getattr(tl, "_scene_job", None)
    if job is None:
        return
    _apply_queued(tl)
    job["timer"].stop()
    from PySide6.QtWidgets import QApplication, QMessageBox
    fn = job.pop("raise_fn", None)
    app = QApplication.instance()
    if fn is not None and app is not None:
        for sig in (app.focusWindowChanged, app.applicationStateChanged):
            try:
                sig.disconnect(fn)
            except (RuntimeError, TypeError):
                pass
    dlg = job["dlg"]
    for sig in (dlg.canceled, dlg.rejected):
        try:
            sig.disconnect()
        except (RuntimeError, TypeError):
            pass
    dlg.close()
    dlg.deleteLater()
    tl._scene_job = None
    import undo
    undo.set_blocked("scenes:%d" % int(tl.sequence_id), False)
    el = time.monotonic() - job["t0"]
    cancelled = job["cancel"].is_set()
    _log("done%s: %d cuts detected, %d slices made, %s scanned of %s in %s (%.1fx realtime)" % (
        " (cancelled)" if cancelled else "", cuts, job["applied"], _hms(job["pos"]),
        _hms(job["info"]["dur"]), _hms(el), job["pos"] / el if el > 0 else 0.0))
    msg = "Scene Detect: %d slices%s" % (job["applied"], " (cancelled)" if cancelled else "")
    tl.status_bar.showMessage(msg, 5000)
    if err and not cancelled:
        QMessageBox.warning(tl, "Scene Detect",
                            "Scene detection stopped (%d slices made):\n\n%s"
                            % (job["applied"], err))

# footage_importer.py
# Imports a media file into the current project's footage table.
# ASCII only.

import os
import sqlite3
import subprocess

_VISUAL_EXTS = {"mov", "mp4", "mkv", "avi", "m4v",
                "png", "jpg", "jpeg", "bmp", "gif", "webp"}


def import_footage(project_db, media_path):
    if not project_db or not os.path.isfile(media_path):
        return None

    filename = os.path.basename(media_path)
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    size_bytes = os.path.getsize(media_path)
    duration = _probe_duration(media_path)

    conn = sqlite3.connect(project_db)
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO footage (original_path, filename, size_bytes, file_type, duration)
        VALUES (?, ?, ?, ?, ?)
    """, (media_path, filename, size_bytes, ext, duration))

    new_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Video / image: width, height, codec of v:0 (ffprobe). Audio stays NULL.
    # Older footage tables without codec: skipped, import still succeeds.
    if ext in _VISUAL_EXTS:
        try:
            from clip_proxy import probe_video_info
            info = probe_video_info(media_path)
            if info is not None:
                conn = sqlite3.connect(project_db)
                try:
                    conn.execute(
                        "UPDATE footage SET width = ?, height = ?, codec = ? WHERE id = ?",
                        (info[0], info[1], info[2], new_id))
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass

    # BUILD 119: MKV field order (progressive / interlaced / telecine /
    # unknown) into footage.field_order. Other files stay NULL. Older
    # footage tables without the column: skipped.
    if ext == "mkv":
        try:
            order = probe_field_order(media_path)
            conn = sqlite3.connect(project_db)
            try:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(footage)")]
                if "field_order" in cols:
                    conn.execute("UPDATE footage SET field_order = ? WHERE id = ?",
                                 (order, new_id))
                    conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    # Ensure sibling audio cache DB exists before peaks/PCM store
    try:
        from peaks import ensure_audiotmp_db
        ensure_audiotmp_db(project_db)
    except Exception:
        pass

    # Waveform peaks for audio and video files; any failure (no ffmpeg, no
    # audio stream) just skips peaks and never fails the import.
    # store_peaks_for_footage also writes mono PCM into _AudioTmp.db.
    try:
        from peaks import store_peaks_for_footage
        store_peaks_for_footage(project_db, new_id, media_path)
    except Exception:
        pass
    return new_id


def _probe_duration(path):
    # Images have no duration
    ext = os.path.splitext(path)[1].lower()
    if ext in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


# ---- BUILD 119: field order probe (MKV import)
#
# 1) ffprobe V:0 field_order (tt / bb / tb / bt = interlaced, progressive,
#    else unknown) plus r_frame_rate / avg_frame_rate.
# 2) idet on a bounded sample (first IDET_SECONDS s, at most IDET_FRAMES
#    frames, video only, -f null), skipped only when ffprobe says
#    progressive at <= 24.5 fps (film rate: neither interlace nor 3:2).
# Classification (idet totals of the last summary):
#    rep   = (repeated top + repeated bottom) / all frames
#    inter = (multi-frame TFF + BFF) / (TFF + BFF + progressive)
#    telecine    if rep >= 0.15 and inter >= 0.2 (3:2 pulldown repeats 2
#                fields every 5 frames, rep ~ 0.40; the synthetic test
#                clip gave rep 0.40, inter 0.99)
#    interlaced  if inter >= 0.5 and rep < 0.15 (true video interlace has
#                no repeated fields; test clip: inter 1.00, rep 0.00)
#    progressive if inter < 0.2 and rep < 0.15 (test clip: 0.00 / 0.00)
#    otherwise, or fewer than IDET_MIN_FRAMES decided frames, or idet
#    failed / timed out: the ffprobe verdict.
# Soft telecine (r_frame_rate ~29.97 with avg_frame_rate ~23.976) is
# telecine without idet.

IDET_SECONDS = 20
IDET_FRAMES = 500
IDET_MIN_FRAMES = 30
IDET_TIMEOUT_S = 60
FIELD_ORDERS = ("progressive", "interlaced", "telecine", "unknown")


def _no_window():
    import sys
    return 0x08000000 if sys.platform.startswith("win") else 0


def _rate(text):
    try:
        if "/" in str(text):
            a, b = str(text).split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def ffprobe_field_info(path):
    # (field_order, r_fps, avg_fps) of stream V:0; ("unknown", 0, 0) on error
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "V:0",
             "-show_entries", "stream=field_order,r_frame_rate,avg_frame_rate",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=20, creationflags=_no_window())
    except Exception:
        return "unknown", 0.0, 0.0
    vals = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return (vals.get("field_order") or "unknown", _rate(vals.get("r_frame_rate")),
            _rate(vals.get("avg_frame_rate")))


def parse_idet(text):
    # Last idet summary -> dict (rep_neither, rep_top, rep_bottom, tff, bff,
    # prog, und) or None
    import re
    rep = re.findall(r"Repeated Fields: Neither:\s*(\d+) Top:\s*(\d+) Bottom:\s*(\d+)", text)
    multi = re.findall(r"Multi frame detection: TFF:\s*(\d+) BFF:\s*(\d+) "
                       r"Progressive:\s*(\d+) Undetermined:\s*(\d+)", text)
    if not rep or not multi:
        return None
    n, t, b = [int(v) for v in rep[-1]]
    tff, bff, prog, und = [int(v) for v in multi[-1]]
    return {"rep_neither": n, "rep_top": t, "rep_bottom": b,
            "tff": tff, "bff": bff, "prog": prog, "und": und}


def run_idet(path):
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-t", str(IDET_SECONDS),
             "-i", path, "-map", "0:V:0", "-an", "-sn", "-dn",
             "-frames:v", str(IDET_FRAMES), "-vf", "idet", "-f", "null", "-"],
            capture_output=True, text=True, timeout=IDET_TIMEOUT_S,
            creationflags=_no_window())
    except Exception:
        return None
    return parse_idet((r.stderr or "") + (r.stdout or ""))


def _ffprobe_verdict(field_order):
    fo = (field_order or "").lower()
    if fo in ("tt", "bb", "tb", "bt"):
        return "interlaced"
    if fo == "progressive":
        return "progressive"
    return "unknown"


def classify_field_order(field_order, r_fps, avg_fps, idet):
    # See the thresholds above. idet None = not run / failed.
    if r_fps and avg_fps and abs(r_fps - 30000.0 / 1001) < 0.05 \
            and abs(avg_fps - 24000.0 / 1001) < 0.05:
        return "telecine"                       # soft telecine flags
    base = _ffprobe_verdict(field_order)
    if not idet:
        return base
    frames = idet["rep_neither"] + idet["rep_top"] + idet["rep_bottom"]
    decided = idet["tff"] + idet["bff"] + idet["prog"]
    if frames < IDET_MIN_FRAMES or decided < IDET_MIN_FRAMES:
        return base
    rep = float(idet["rep_top"] + idet["rep_bottom"]) / frames
    inter = float(idet["tff"] + idet["bff"]) / decided
    if rep >= 0.15 and inter >= 0.2:
        return "telecine"
    if inter >= 0.5 and rep < 0.15:
        return "interlaced"
    if inter < 0.2 and rep < 0.15:
        return "progressive"
    return base


def needs_idet(field_order, r_fps):
    return not ((field_order or "").lower() == "progressive"
                and 0 < (r_fps or 0) <= 24.5)


def probe_field_order(path):
    # One of FIELD_ORDERS. Bounded: ffprobe (20 s timeout) + idet on at
    # most IDET_FRAMES frames / IDET_SECONDS s (IDET_TIMEOUT_S timeout).
    fo, r_fps, avg_fps = ffprobe_field_info(path)
    idet = run_idet(path) if needs_idet(fo, r_fps) else None
    result = classify_field_order(fo, r_fps, avg_fps, idet)
    # print("FIELD ORDER %s: ffprobe=%s r=%.3f avg=%.3f idet=%s -> %s"
    #       % (path, fo, r_fps, avg_fps, idet, result))
    return result

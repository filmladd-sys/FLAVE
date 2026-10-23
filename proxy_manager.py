# proxy_manager.py
# Proxies for footage: ProxyPath setting and a read-only Proxies list
# window. The Proxies table and the ProxyPath setting are created by
# new_project.py _init_db.
# The window's 'Set All Clips' box sets the proxy of every clip of the
# footage in every sequence (set_all_clips_proxy).
# ASCII only.

import os
import sqlite3
import time

PROXY_PATH_KEY = "ProxyPath"


def _project_folder(db_path):
    return os.path.dirname(os.path.abspath(db_path)) if db_path else ""


def get_proxy_path(db_path):
    # Stored ProxyPath, or the project folder if missing / unreadable
    if not db_path:
        return ""
    value = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key = ?",
                               (PROXY_PATH_KEY,)).fetchone()
            value = row[0] if row else None
        finally:
            conn.close()
    except Exception:
        value = None
    return value if value else _project_folder(db_path)


def prune_missing_proxies(db_path):
    # Run once per project open (project_manager). Deletes each Proxies
    # row whose proxy file is gone, in one transaction, then resets any
    # footage / clips proxy_percent that pointed at a percent with no
    # Proxies row left for that footage to 0 (None: decode the original,
    # the same fallback resolve_proxies uses for a missing file). The
    # Proxy Manager then offers that percent again, so deleting a proxy
    # file is a way to regenerate it. Paths are checked as stored, the way
    # resolve_proxies opens them. Rows are kept (not checked) when the
    # path is not absolute or its folder does not exist (e.g. a
    # disconnected drive), so an unavailable drive never removes entries.
    # Proxy files are never touched. Returns the number of rows removed;
    # never raises.
    if not db_path or not os.path.isfile(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path)
    except Exception as e:
        print("Proxy check skipped: %s" % e)
        return 0
    removed = 0
    try:
        rows = conn.execute(
            "SELECT id, footage_id, path, percentage FROM Proxies").fetchall()
        gone, kept_dirs = [], set()
        for pid, fid, path, pct in rows:
            path = path or ""
            if path and os.path.isfile(path):
                continue
            folder = os.path.dirname(path)
            if not path or not os.path.isabs(path):
                print("Proxy check skipped (path not absolute): %s" % (path or "(empty)"))
                continue
            if not os.path.isdir(folder):
                kept_dirs.add(folder)
                continue
            gone.append((pid, fid, path, pct))
        for folder in sorted(kept_dirs):
            print("Proxy check skipped (folder not available): %s" % folder)
        if not gone:
            print("Proxy check: %d entr%s, none removed" % (
                len(rows), "y" if len(rows) == 1 else "ies"))
            return 0
        with conn:
            for pid, fid, path, pct in gone:
                conn.execute("DELETE FROM Proxies WHERE id = ?", (int(pid),))
                print("Proxy entry removed (file missing): %s" % path)
            for fid, pct in sorted(set((r[1], r[3]) for r in gone)):
                left = conn.execute(
                    "SELECT 1 FROM Proxies WHERE footage_id = ? AND percentage = ? "
                    "LIMIT 1", (fid, pct)).fetchone()
                if left:
                    continue
                conn.execute("UPDATE footage SET proxy_percent = 0 WHERE id = ? "
                             "AND proxy_percent = ?", (fid, pct))
                conn.execute("UPDATE clips SET proxy_percent = 0 WHERE "
                             "footage_id = ? AND proxy_percent = ?", (fid, pct))
        removed = len(gone)
    except Exception as e:
        print("Proxy check failed: %s: %s" % (type(e).__name__, e))
        return 0
    finally:
        conn.close()
    print("Proxy check: %d missing proxy entr%s removed"
          % (removed, "y" if removed == 1 else "ies"))
    return removed


def proxy_counts(db_path):
    # {footage_id: number of Proxies rows}; {} on a read error
    if not db_path:
        return {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT footage_id, COUNT(*) FROM Proxies GROUP BY footage_id"
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    return {int(fid): int(n) for fid, n in rows}


def proxy_rows(db_path, footage_id):
    # [(id, path, percentage)] for footage_id; [] on a read error
    if not db_path:
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT id, path, percentage FROM Proxies WHERE footage_id = ? "
                "ORDER BY id", (int(footage_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


PROXY_PERCENTAGES = (75, 50, 25, 10)
MKV_PROXY_PERCENTAGES = (100, 75, 50, 25, 10)   # BUILD 119: 100% for MKV only

# BUILD 119: proxy modes. 'deint' keeps the source frame count; 'deint2x'
# (one frame per field) and 'ivtc' (3:2 pulldown removed, 24000/1001)
# change it, so the composite maps source frames to proxy frames by time.
PROXY_MODES = ("normal", "deint", "deint2x", "ivtc")
MODE_LABELS = {"normal": "Normal", "deint": "Deinterlace",
               "deint2x": "Deinterlace (double rate)",
               "ivtc": "Inverse telecine"}
MODE_SHORT = {"normal": "normal", "deint": "deint", "deint2x": "deint 2x",
              "ivtc": "IVTC"}


def proxy_rows_full(db_path, footage_id):
    # [(id, path, pct, mode, deinterlace, proxy_fps, proxy_frames)]; [] on
    # a read error
    if not db_path:
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT id, path, percentage, COALESCE(proxy_mode, 'normal'), "
                "COALESCE(deinterlace, 0), proxy_fps, proxy_frames FROM Proxies "
                "WHERE footage_id = ? ORDER BY id", (int(footage_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def footage_field_order(db_path, footage_id):
    # footage.field_order (BUILD 119 MKV probe) or None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT field_order FROM footage WHERE id = ?",
                               (int(footage_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return row[0] if row else None


def is_mkv_path(path):
    return os.path.splitext(path or "")[1].lower() == ".mkv"


def valid_modes(src_path, field_order):
    # Modes offered for a footage: Normal always; MKV interlaced adds
    # Deinterlace and double rate; MKV telecine adds Inverse telecine.
    out = ["normal"]
    if is_mkv_path(src_path):
        if field_order == "interlaced":
            out += ["deint", "deint2x"]
        elif field_order == "telecine":
            out.append("ivtc")
    return out


def percentages_for(src_path):
    return MKV_PROXY_PERCENTAGES if is_mkv_path(src_path) else PROXY_PERCENTAGES


def mode_text(mode, fps, frames):
    # Proxy Manager list: 'deint 2x 59.94 fps 600 f'
    t = MODE_SHORT.get(mode or "normal", mode or "normal")
    if mode and mode != "normal":
        if fps:
            t += " %.2f fps" % float(fps)
        if frames:
            t += " %d f" % int(frames)
    return t
VIDEO_PROXY_EXT = "mp4"     # video footage proxies
IMAGE_PROXY_EXT = "jpg"     # image footage proxies
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def footage_source(db_path, footage_id):
    # (original_path, filename) for footage_id, or ("", "")
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT original_path, filename FROM footage WHERE id = ?",
                               (int(footage_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    if not row:
        return "", ""
    return row[0] or "", row[1] or ""


def proxy_file_path(proxy_dir, source_path, percentage):
    # <ProxyPath>/<footage stem>_proxy<pct>.<mp4 for video | jpg for image>
    base = os.path.basename(source_path or "")
    stem, ext = os.path.splitext(base)
    out_ext = IMAGE_PROXY_EXT if ext.lower() in _IMAGE_EXTS else VIDEO_PROXY_EXT
    return os.path.join(proxy_dir, "%s_proxy%d.%s" % (stem, int(percentage), out_ext))


def available_percentages(db_path, footage_id):
    have = set(int(r[2]) for r in proxy_rows(db_path, footage_id))
    return [p for p in PROXY_PERCENTAGES if p not in have]


def set_default_after_generate(db_path, footage_id, percentage, ok, out_path=None):
    # BUILD 122: after a successful generate, footage.proxy_percent (the
    # Footage Manager 'Proxy Default') = the new percent, written through
    # clip_proxy.set_footage_proxy_percent (same path as the Proxy Default
    # menu: undo snapshot 'Proxy default').
    # Video: ok and the proxy file exists. Image: ok (placeholder row, no
    # file). Clips keep their own proxy_percent. Only the percent is stored;
    # with Normal and Deinterlace at that percent the composite and the
    # BUILD 120 label use the preferred mode (e.g. '50% (deint)').
    # Returns True if the default was written.
    if not ok:
        return False
    if out_path is not None and not os.path.isfile(out_path):
        return False
    try:
        from clip_proxy import set_footage_proxy_percent
        return bool(set_footage_proxy_percent(db_path, footage_id, int(percentage)))
    except Exception:
        return False


def add_proxy_row(db_path, footage_id, percentage):
    # Insert one Proxies row (no file is written). Returns the new id.
    src, name = footage_source(db_path, footage_id)
    path = proxy_file_path(get_proxy_path(db_path), src or name, percentage)
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO Proxies (footage_id, path, percentage) VALUES (?, ?, ?)",
            (int(footage_id), path, int(percentage)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---- video proxies (FFmpeg CUDA decode + scale_cuda + hevc_nvenc, BUILD 87)

_VIDEO_EXTS = (".mov", ".mp4", ".mkv", ".avi", ".m4v")


def is_video_path(path):
    return os.path.splitext(path or "")[1].lower() in _VIDEO_EXTS


def video_proxy_path(proxy_dir, source_path, percentage, mode="normal"):
    # <ProxyPath>/<source basename>.<pct>.mp4, e.g. VidTest.mov.50.mp4.
    # BUILD 119: deint / ivtc -> .<pct>.deint.mp4; double rate (same
    # percent as a deint one) -> .<pct>.deint2x.mp4
    tail = {"deint": ".deint", "ivtc": ".deint", "deint2x": ".deint2x"}.get(mode, "")
    return os.path.join(proxy_dir, os.path.basename(source_path) + "." +
                        str(int(percentage)) + tail + ".mp4")


# Smallest proxy side the NVDEC HEVC decoder accepts (its minimum coded
# size is 144 x 144). NVENC encodes smaller frames without complaint, but
# a proxy below this cannot be decoded (cuvidCreateDecoder fails), so
# scaled_size never makes one and resolve_proxies skips existing ones.
PROXY_MIN_SIDE = 144


def scaled_size(width, height, percentage):
    # Percent of each side, rounded to even, minimum 2. 100% = the source
    # size cut down to even (no scale-down). A percent whose short side
    # falls below PROXY_MIN_SIDE is raised so the short side is
    # PROXY_MIN_SIDE (aspect kept, even sides, never larger than the
    # source); a source whose short side is already at or below
    # PROXY_MIN_SIDE gives the source size (never upscaled). The file name
    # keeps the requested percent; the player scales by the real decoded
    # size, not by the percent.
    src_w = max(2, int(width) - int(width) % 2)
    src_h = max(2, int(height) - int(height) % 2)
    if int(percentage) >= 100:
        return src_w, src_h

    def side(v, f):
        s = int(round(float(v) * f / 2.0)) * 2
        return max(2, s)
    f = int(percentage) / 100.0
    w, h = side(width, f), side(height, f)
    if min(w, h) >= PROXY_MIN_SIDE:
        return w, h
    short = min(int(width), int(height))
    if short <= PROXY_MIN_SIDE:
        return src_w, src_h
    f = float(PROXY_MIN_SIDE) / float(short)
    w, h = side(width, f), side(height, f)
    return min(w, src_w), min(h, src_h)


def decodable_size(size):
    # False only for a known (width, height) with a side below PROXY_MIN_SIDE
    try:
        return size is None or min(int(size[0]), int(size[1])) >= PROXY_MIN_SIDE
    except Exception:
        return True


def _no_window_flags():
    import sys
    return 0x08000000 if sys.platform.startswith("win") else 0   # CREATE_NO_WINDOW


def _run(cmd):
    # (returncode, stdout text, stderr text); raises OSError if not found
    import subprocess
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_no_window_flags())
    return (proc.returncode, proc.stdout.decode("ascii", "replace"),
            proc.stderr.decode("ascii", "replace"))


PROXY_ENCODER = "hevc_nvenc"   # every proxy


def choose_encoder(w, h):
    # Every proxy is HEVC NVENC (kept as a function for callers)
    return PROXY_ENCODER


def nvenc_available(encoder=PROXY_ENCODER, w=None, h=None):
    # (ok, message). ffmpeg on PATH and encoder listed in -encoders
    why = ""
    if w is not None and h is not None:
        why = " (needed for the %dx%d proxy)" % (int(w), int(h))
    try:
        code, out, err = _run(["ffmpeg", "-hide_banner", "-encoders"])
    except OSError:
        return False, "ffmpeg was not found on PATH, so %s%s cannot run." % (encoder, why)
    if encoder not in out + err:
        return False, ("ffmpeg on PATH has no %s encoder (NVIDIA NVENC)%s." %
                       (encoder, why))
    return True, ""


def probe_size(src):
    # (width, height) of the first video stream via ffprobe, or None
    # (BUILD 118: V:0 skips attached pictures / cover art)
    try:
        code, out, _err = _run([
            "ffprobe", "-v", "error", "-select_streams", "V:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", src])
    except OSError:
        return None
    if code != 0:
        return None
    try:
        w, h = out.strip().splitlines()[0].split("x")[:2]
        return int(w), int(h)
    except Exception:
        return None


def footage_size(db_path, footage_id):
    # Stored footage width / height, or None
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT width, height FROM footage WHERE id = ?",
                               (int(footage_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row and row[0] and row[1]:
        return int(row[0]), int(row[1])
    return None


def ivtc_rate(source_fps):
    # decimate keeps 4 of 5 frames: 29.97 -> 24000/1001
    try:
        f = float(source_fps or 0)
    except (TypeError, ValueError):
        f = 0.0
    if f <= 0 or abs(f - 30000.0 / 1001) < 0.01:
        return "24000/1001"
    from fractions import Fraction
    fr = Fraction(f * 4.0 / 5.0).limit_denominator(1001)
    return "%d/%d" % (fr.numerator, fr.denominator)


def build_proxy_cmd(src, out, w, h, mode="normal", source_fps=None):
    # BUILD 119 modes (all: first real video stream, no audio / subs):
    #   deint   / deint2x: GPU decode, yadif_cuda mode 0 / 1 (one frame per
    #           field), scale_cuda, hevc_nvenc
    #   ivtc:   CPU fieldmatch + yadif (leftover combed frames) + decimate,
    #           -r 24000/1001, then hwupload_cuda + scale_cuda, hevc_nvenc
    #   normal: unchanged (below)
    tail = [ "-c:v", PROXY_ENCODER, "-preset", "p4", "-g", "15", "-tag:v", "hvc1", out]
    scale = "scale_cuda=%d:%d:format=yuv420p" % (int(w), int(h))
    if mode in ("deint", "deint2x"):
        return (["ffmpeg", "-y", "-hide_banner", "-hwaccel", "cuda",
                 "-hwaccel_output_format", "cuda", "-i", src, "-map", "0:V:0",
                 "-an", "-sn", "-dn", "-vf",
                 "yadif_cuda=mode=%d:deint=all,%s" % (1 if mode == "deint2x" else 0, scale)]
                + tail)
    if mode == "ivtc":
        return (["ffmpeg", "-y", "-hide_banner", "-init_hw_device", "cuda=cu",
                 "-filter_hw_device", "cu", "-i", src, "-map", "0:V:0",
                 "-an", "-sn", "-dn", "-vf",
                 "fieldmatch,yadif=deint=interlaced,decimate,format=nv12,"
                 "hwupload_cuda,%s" % scale, "-r", ivtc_rate(source_fps)]
                + tail)
    return _build_normal_cmd(src, out, w, h)


# BUILD 121: CPU-decode fallback. Sources NVDEC cannot take (e.g. a bgra
# PNG-in-AVI logo: GPU decode silently falls back to software frames and
# scale_cuda then fails with 'Error reinitializing filters' / 'Function not
# implemented') are decoded and scaled on the CPU, still encoded with
# hevc_nvenc: same output file, same Proxies row, still HEVC + hvc1.
NVDEC_CODECS = ("h264", "hevc", "mpeg1video", "mpeg2video", "mpeg4", "vc1",
                "vp8", "vp9", "av1", "mjpeg")
# Codecs NVDEC also decodes at high bit depth (4:2:0 10/12-bit; HEVC 4:4:4)
_NVDEC_HIGH_DEPTH = ("hevc", "vp9", "av1")
GPU_FAIL_PATTERNS = (
    "error reinitializing filters", "function not implemented",
    "impossible to convert between the formats", "hwaccel", "cuvid",
    "no decoder surfaces", "scale_cuda", "yadif_cuda", "hwupload",
    "failed to inject frame", "error while filtering", "nvdec",
)


def probe_codec(src):
    # (codec_name, pix_fmt) of the first real video stream, or (None, None)
    try:
        code, out, _err = _run([
            "ffprobe", "-v", "error", "-select_streams", "V:0",
            "-show_entries", "stream=codec_name,pix_fmt",
            "-of", "default=noprint_wrappers=1", src])
    except OSError:
        return None, None
    vals = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return (vals.get("codec_name") or None), (vals.get("pix_fmt") or None)


def gpu_decodable(codec, pix_fmt):
    # (ok, reason). Unknown codec / pix_fmt: try the GPU (the stderr retry
    # catches a miss). Rejected: codecs NVDEC has no decoder for; 4:2:2,
    # 4:1:1, 4:4:0, alpha / RGB / palette formats; high bit depth or 4:4:4
    # except for HEVC / VP9 / AV1 (H.264 High 10 / 4:2:2 / 4:4:4 included).
    if not codec:
        return True, "codec not probed"
    c = codec.lower()
    if c not in NVDEC_CODECS:
        return False, "codec %s has no NVDEC decoder" % codec
    if not pix_fmt:
        return True, ""
    f = pix_fmt.lower()
    if f in ("gray", "gray8"):
        return True, ""
    if (f.startswith(("rgb", "bgr", "argb", "abgr", "gbr", "pal", "ya", "yuva"))
            or "422" in f or "411" in f or "440" in f):
        return False, "%s %s is not NVDEC-decodable" % (codec, pix_fmt)
    high = any(t in f for t in ("p9", "p10", "p12", "p14", "p16", "p010", "p016"))
    if "444" in f and c != "hevc":
        return False, "%s %s is not NVDEC-decodable" % (codec, pix_fmt)
    if high and c not in _NVDEC_HIGH_DEPTH:
        return False, "%s %s (high bit depth) is not NVDEC-decodable" % (codec, pix_fmt)
    return True, ""


def is_gpu_failure(stderr_text):
    # stderr of a failed GPU attempt names a decode / filter / hwaccel
    # error: the first matching pattern, else None (no retry)
    t = (stderr_text or "").lower()
    for pat in GPU_FAIL_PATTERNS:
        if pat in t:
            return pat
    return None


def build_proxy_cmd_cpu(src, out, w, h, mode="normal", source_fps=None):
    # CPU decode + CPU filters (yadif / fieldmatch,decimate) + CPU scale to
    # the same even size, encoded by hevc_nvenc. No -hwaccel, no *_cuda,
    # no hwupload.
    scale = "scale=%d:%d,format=yuv420p" % (int(w), int(h))
    if mode in ("deint", "deint2x"):
        vf = "yadif=mode=%d:deint=all,%s" % (1 if mode == "deint2x" else 0, scale)
    elif mode == "ivtc":
        vf = "fieldmatch,yadif=deint=interlaced,decimate," + scale
    else:
        vf = scale
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", src, "-map", "0:V:0", "-an",
           "-sn", "-dn", "-vf", vf]
    if mode == "ivtc":
        cmd += ["-r", ivtc_rate(source_fps)]
    return cmd + ["-c:v", PROXY_ENCODER, "-preset", "p4", "-g", "15",
                  "-tag:v", "hvc1", out]


def plan_proxy_cmds(src, out, w, h, mode="normal", source_fps=None, probe=None):
    # (first_cmd, fallback_cmd_or_None, note). GPU first with the CPU
    # command as the one retry; a source NVDEC cannot take goes straight
    # to the CPU command (no retry). probe: (codec_name, pix_fmt).
    codec, pix_fmt = probe if probe is not None else probe_codec(src)
    ok, why = gpu_decodable(codec, pix_fmt)
    cpu = build_proxy_cmd_cpu(src, out, w, h, mode, source_fps)
    if not ok:
        return cpu, None, why
    return build_proxy_cmd(src, out, w, h, mode, source_fps), cpu, ""


def _build_normal_cmd(src, out, w, h):
    # User's exact shape (GPU decode, scale_cuda, hevc_nvenc) plus -tag:v hvc1
    # so MP4 players recognise the HEVC track. No CPU filter; the 8-bit
    # conversion is scale_cuda format=yuv420p (no -pix_fmt: it would force a
    # software auto_scale from CUDA frames and fail).
    # BUILD 118: first real video stream only (0:V:0 skips cover art); no
    # audio, subtitle, data or attachment streams (MKV safe).
    return ["ffmpeg", "-y", "-hide_banner", "-hwaccel", "cuda",
            "-hwaccel_output_format", "cuda", "-i", src, "-map", "0:V:0", "-an",
            "-sn", "-dn",
            "-vf", "scale_cuda=%d:%d:format=yuv420p" % (int(w), int(h)),
            "-c:v", PROXY_ENCODER, "-preset", "p4",
            "-g", "15", "-tag:v", "hvc1", out]


def probe_rate_frames(path):
    # (fps, frame count) of stream V:0 (packets counted, no decode)
    try:
        code, out, _err = _run([
            "ffprobe", "-v", "error", "-select_streams", "V:0", "-count_packets",
            "-show_entries", "stream=r_frame_rate,nb_read_packets",
            "-of", "default=noprint_wrappers=1", path])
    except OSError:
        return None, None
    vals = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    fps = None
    try:
        r = vals.get("r_frame_rate", "")
        if "/" in r:
            a, b = r.split("/", 1)
            fps = float(a) / float(b) if float(b) else None
        elif r:
            fps = float(r)
    except ValueError:
        fps = None
    try:
        frames = int(vals.get("nb_read_packets", ""))
    except ValueError:
        frames = None
    return fps, frames


def save_proxy_row(db_path, footage_id, path, percentage, mode="normal",
                   proxy_fps=None, proxy_frames=None, source_fps=None):
    # Update the path (and mode columns) of an existing row for footage +
    # pct + mode, else insert
    deint = 0 if mode == "normal" else 1
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM Proxies WHERE footage_id = ? AND percentage = ? "
            "AND COALESCE(proxy_mode, 'normal') = ? ORDER BY id LIMIT 1",
            (int(footage_id), int(percentage), mode)).fetchone()
        vals = (path, deint, mode, proxy_fps, proxy_frames, source_fps)
        if row:
            conn.execute(
                "UPDATE Proxies SET path = ?, deinterlace = ?, proxy_mode = ?, "
                "proxy_fps = ?, proxy_frames = ?, source_fps = ? WHERE id = ?",
                vals + (row[0],))
            rid = row[0]
        else:
            rid = conn.execute(
                "INSERT INTO Proxies (path, deinterlace, proxy_mode, proxy_fps, "
                "proxy_frames, source_fps, footage_id, percentage) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                vals + (int(footage_id), int(percentage))).lastrowid
        conn.commit()
        return rid
    finally:
        conn.close()


def _remove_partial(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def _run_proxy_ffmpeg(cmd, wait=None):
    # (exit code, stderr text); (None, reason) if ffmpeg could not start.
    # wait(proc) is called while ffmpeg runs (GUI keeps processing events).
    import subprocess
    import tempfile
    err_file = tempfile.TemporaryFile()
    try:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err_file,
                                    creationflags=_no_window_flags())
        except OSError as e:
            return None, str(e)
        if wait is not None:
            wait(proc)
        code = proc.wait()
        err_file.seek(0)
        err = err_file.read().decode("ascii", "replace")
    finally:
        err_file.close()
    return code, err


def _proxy_ok(code, out):
    return code == 0 and os.path.isfile(out) and os.path.getsize(out) > 0


def _ffmpeg_error_text(code, cmd, err, title=None):
    import subprocess
    tail = "\n".join((err or "").strip().splitlines()[-15:])
    head = ("%s: ffmpeg failed" % title) if title else "ffmpeg failed"
    return "%s (exit code %s)\n\nCommand:\n%s\n\nstderr (last 15 lines):\n%s" % (
        head, code, subprocess.list2cmdline(cmd), tail)


def generate_video_proxy(db_path, footage_id, percentage, wait=None, mode="normal"):
    # Run ffmpeg; on success save the Proxies row. Returns (ok, message).
    # BUILD 121: GPU command first unless the ffprobe codec / pix_fmt is not
    # NVDEC-decodable (then CPU decode + CPU scale only). A GPU failure whose
    # stderr names a filter / hwaccel error is retried once on the CPU path
    # (partial output deleted first); both failing shows both commands.
    src, _name = footage_source(db_path, footage_id)
    if not src or not os.path.isfile(src):
        return False, "Source file not found:\n%s" % src
    size = probe_size(src) or footage_size(db_path, footage_id)
    if size is None:
        return False, "Could not read the source width and height (ffprobe)."
    w, h = scaled_size(size[0], size[1], percentage)
    source_fps = probe_rate_frames(src)[0] if mode != "normal" else None
    ok, msg = nvenc_available(choose_encoder(w, h), w, h)
    if not ok:
        return False, msg
    proxy_dir = get_proxy_path(db_path)
    try:
        os.makedirs(proxy_dir, exist_ok=True)
    except Exception as e:
        return False, "Could not create ProxyPath %s: %s" % (proxy_dir, e)
    out = video_proxy_path(proxy_dir, src, percentage, mode)
    cmd, fallback, note = plan_proxy_cmds(src, out, w, h, mode, source_fps)
    t0 = time.perf_counter()
    if fallback is None:
        print("WORKER proxy: %s -> CPU decode + CPU scale (%s)"
              % (os.path.basename(src), note))
    # print("PROXY cmd: %s" % " ".join(cmd))
    code, err = _run_proxy_ffmpeg(cmd, wait)
    if code is None:
        return False, "Could not start ffmpeg: %s" % err
    if not _proxy_ok(code, out):
        _remove_partial(out)
        pat = is_gpu_failure(err) if fallback is not None else None
        if pat is None:
            return False, _ffmpeg_error_text(code, cmd, err)
        print("WORKER proxy: GPU attempt failed after %.1fs (exit %s, stderr '%s'); "
              "retrying with CPU decode + CPU scale" % (time.perf_counter() - t0, code, pat))
        t1 = time.perf_counter()
        code2, err2 = _run_proxy_ffmpeg(fallback, wait)
        if code2 is None:
            return False, (_ffmpeg_error_text(code, cmd, err, "GPU attempt") +
                           "\n\nCPU fallback: could not start ffmpeg: %s" % err2)
        if not _proxy_ok(code2, out):
            _remove_partial(out)
            return False, (_ffmpeg_error_text(code, cmd, err, "GPU attempt") + "\n\n" +
                           _ffmpeg_error_text(code2, fallback, err2, "CPU fallback"))
        print("WORKER proxy: CPU fallback done in %.1fs" % (time.perf_counter() - t1))
    else:
        print("WORKER proxy: %s done in %.1fs" % (os.path.basename(out),
                                                  time.perf_counter() - t0))
    try:
        pfps, pframes = probe_rate_frames(out)
        sfps = source_fps if source_fps else probe_rate_frames(src)[0]
        save_proxy_row(db_path, footage_id, out, percentage, mode, pfps, pframes, sfps)
    except Exception as e:
        return False, "Proxy file written but the row could not be saved: %s" % e
    return True, out


# Clips of a footage on video / image tracks (the clips that carry a proxy,
# as for a new clip and the timeline 'Proxy' submenu) whose proxy differs
_ALL_CLIPS_WHERE = (
    "footage_id = ? AND COALESCE(proxy_percent, 0) <> ? AND track_id IN "
    "(SELECT id FROM tracks WHERE LOWER(COALESCE(track_type, '')) "
    "IN ('video', 'image'))")


def set_all_clips_proxy(db_path, footage_id, pct):
    # BUILD 291: clips.proxy_percent = pct for every video / image clip of
    # footage_id in every sequence (0 = None: the original). One undo step, taken only
    # when some clip changes. Returns the number of clips changed.
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE " + _ALL_CLIPS_WHERE,
            (int(footage_id), int(pct))).fetchone()[0]
    finally:
        conn.close()
    if not n:
        return 0
    try:
        from undo import push_undo
        push_undo(db_path, "Set all clips proxy")
    except Exception:
        pass
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE clips SET proxy_percent = ? WHERE " + _ALL_CLIPS_WHERE,
            (int(pct), int(footage_id), int(pct)))
        conn.commit()
    finally:
        conn.close()
    return int(n)


def refresh_open_timelines(db_path, message):
    # Every open timeline of this project reloads (same path as a clip
    # proxy change): the reload's mile-marker diff re-resolves proxies and
    # redraws the player frames that changed
    try:
        import sequence_timeline
        timelines = list(sequence_timeline._open_windows.values())
    except Exception:
        return
    for tl in timelines:
        try:
            if os.path.abspath(tl._project_db()) == os.path.abspath(db_path):
                tl._after_clip_change(message)
        except Exception as e:
            print(f"Proxy refresh failed: {e}")


def show_message(parent, title, text):
    # Error / info dialog: selectable monospace text, Copy and Close
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton,
        QApplication,
    )
    from PySide6.QtGui import QFontDatabase
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setSizeGripEnabled(True)
    dlg.resize(720, 360)
    layout = QVBoxLayout(dlg)
    edit = QPlainTextEdit()
    edit.setReadOnly(True)
    edit.setPlainText(text or "")
    edit.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
    layout.addWidget(edit, 1)
    row = QHBoxLayout()
    row.addStretch(1)
    copy_btn = QPushButton("Copy")
    copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(edit.toPlainText()))
    row.addWidget(copy_btn)
    close_btn = QPushButton("Close")
    close_btn.setDefault(True)
    close_btn.clicked.connect(dlg.accept)
    row.addWidget(close_btn)
    layout.addLayout(row)
    dlg.exec()


_open_windows = []   # keeps windows alive (no garbage collection)


def open_proxy_manager(parent, db_path, footage_id, footage_name, on_change=None):
    from PySide6.QtWidgets import (
        QMainWindow, QTableWidget, QTableWidgetItem, QHeaderView, QWidget,
        QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton, QGroupBox,
    )

    src, fname = footage_source(db_path, footage_id)
    title_name = os.path.basename(src) if src else (footage_name or fname)

    class ProxyManagerWindow(QMainWindow):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("Proxy Manager: %s" % title_name)
            self.resize(760, 340)
            central = QWidget()
            layout = QVBoxLayout(central)
            row = QHBoxLayout()
            row.addWidget(QLabel("Generate"))
            self.combo = QComboBox()
            row.addWidget(self.combo, 1)
            # BUILD 119: Mode (Normal / Deinterlace / double rate / IVTC),
            # shown only when the footage has more than Normal
            self.mode_label = QLabel("Mode")
            row.addWidget(self.mode_label)
            self.mode_combo = QComboBox()
            row.addWidget(self.mode_combo, 1)
            layout.addLayout(row)
            self.gen_btn = QPushButton("Generate")
            self.gen_btn.clicked.connect(self._generate)
            layout.addWidget(self.gen_btn)
            self.combo.currentIndexChanged.connect(self._fill_modes)
            self.mode_combo.currentIndexChanged.connect(self._update_button)
            self.table = QTableWidget(0, 4)
            self.table.setHorizontalHeaderLabels(["id", "path", "percentage", "mode"])
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            layout.addWidget(self.table, 1)
            # BUILD 291: Set All Clips: None (original) or a percent that
            # already has a proxy for this footage (clip_proxy.proxy_options;
            # nothing is generated here). Refilled by _reload.
            box = QGroupBox("Set All Clips")
            box_row = QHBoxLayout(box)
            box_row.addWidget(QLabel("Proxy"))
            self.all_combo = QComboBox()
            box_row.addWidget(self.all_combo, 1)
            self.all_btn = QPushButton("Apply")
            self.all_btn.clicked.connect(self._apply_all)
            box_row.addWidget(self.all_btn)
            self.all_status = QLabel("")
            box_row.addWidget(self.all_status, 2)
            layout.addWidget(box)
            self.setCentralWidget(central)
            self._reload()

        def _reload(self):
            rows = proxy_rows_full(db_path, footage_id)
            self.table.setRowCount(len(rows))
            for i, (pid, path, pct, mode, _deint, pfps, pframes) in enumerate(rows):
                self.table.setItem(i, 0, QTableWidgetItem(str(pid)))
                self.table.setItem(i, 1, QTableWidgetItem(path or ""))
                self.table.setItem(i, 2, QTableWidgetItem(str(pct)))
                self.table.setItem(i, 3, QTableWidgetItem(mode_text(mode, pfps, pframes)))
            src_path, _n = footage_source(db_path, footage_id)
            self._modes = valid_modes(src_path, footage_field_order(db_path, footage_id))
            self._have = set((int(r[2]), r[3] or "normal") for r in rows)
            self.combo.blockSignals(True)
            self.combo.clear()
            for pct in percentages_for(src_path):
                if any((pct, m) not in self._have for m in self._modes):
                    self.combo.addItem("%d%%" % pct, pct)
            self.combo.blockSignals(False)
            show = len(self._modes) > 1
            self.mode_label.setVisible(show)
            self.mode_combo.setVisible(show)
            self._fill_modes()
            self._fill_all_combo()

        def _fill_all_combo(self):
            # Keeps the chosen percent when it is still listed
            from clip_proxy import proxy_options, proxy_kinds, proxy_label
            keep = self.all_combo.currentData()
            kinds = proxy_kinds(db_path, footage_id)
            self.all_combo.blockSignals(True)
            self.all_combo.clear()
            for pct in proxy_options(db_path, footage_id):
                text = proxy_label(pct, kinds.get(int(pct))) if pct else "None (original)"
                self.all_combo.addItem(text, int(pct))
            i = self.all_combo.findData(keep) if keep is not None else -1
            self.all_combo.setCurrentIndex(i if i >= 0 else 0)
            self.all_combo.blockSignals(False)

        def _apply_all(self):
            pct = self.all_combo.currentData()
            if pct is None:
                return
            text = self.all_combo.currentText()
            try:
                n = set_all_clips_proxy(db_path, footage_id, int(pct))
            except Exception as e:
                show_message(self, "Proxy Manager", "Could not set clips: %s" % e)
                return
            self.all_status.setText("%d clip(s) set to %s" % (n, text))
            if n:
                refresh_open_timelines(
                    db_path, "Proxy: %d clip(s) of %s set to %s"
                    % (n, title_name, text))

        def _fill_modes(self, *_args):
            pct = self.combo.currentData()
            self.mode_combo.blockSignals(True)
            self.mode_combo.clear()
            if pct is not None:
                for m in self._modes:
                    if (int(pct), m) not in self._have:
                        self.mode_combo.addItem(MODE_LABELS[m], m)
            self.mode_combo.blockSignals(False)
            self._update_button()

        def _update_button(self, *_args):
            self.gen_btn.setEnabled(not getattr(self, "_generating", False)
                                    and self.combo.currentData() is not None
                                    and self.mode_combo.currentData() is not None)

        def _generate(self):
            pct = self.combo.currentData()
            mode = self.mode_combo.currentData() or "normal"
            if pct is None or getattr(self, "_generating", False):
                return
            src_path, _n = footage_source(db_path, footage_id)
            if is_video_path(src_path):
                ok, out = self._generate_video(int(pct), mode)
                # BUILD 122: success -> footage Proxy Default = this percent
                set_default_after_generate(db_path, footage_id, int(pct), ok,
                                           out if ok else None)
            else:
                # Images: BUILD 82 placeholder row only (no file written)
                try:
                    add_proxy_row(db_path, footage_id, int(pct))
                except Exception as e:
                    show_message(self, "Proxy Manager",
                                        "Could not add proxy: %s" % e)
                    return
                set_default_after_generate(db_path, footage_id, int(pct), True)
            # on_change: Footage Manager refresh (Proxy Default / #Proxies)
            self._reload()
            if on_change is not None:
                try:
                    on_change()
                except Exception:
                    pass

        def _generate_video(self, pct, mode="normal"):
            from PySide6.QtWidgets import QApplication
            from PySide6.QtCore import Qt

            def wait(proc):
                while proc.poll() is None:
                    QApplication.processEvents()
                    time.sleep(0.05)

            # Busy state on this window only (other windows stay usable)
            self._generating = True
            self.gen_btn.setEnabled(False)
            self.gen_btn.setText("Generating")
            self.combo.setEnabled(False)
            self.mode_combo.setEnabled(False)
            self.setCursor(Qt.WaitCursor)
            ok, msg = False, ""
            try:
                try:
                    ok, msg = generate_video_proxy(db_path, footage_id, pct, wait, mode)
                except Exception as e:
                    ok, msg = False, "Proxy generation failed: %s" % e
            finally:
                self.unsetCursor()
                self._generating = False
                self.gen_btn.setText("Generate")
                self.combo.setEnabled(True)
                self.mode_combo.setEnabled(True)
                self._update_button()
            if not ok:
                show_message(self, "Proxy Manager", msg)
            return ok, msg

        def closeEvent(self, event):
            try:
                _open_windows.remove(self)
            except ValueError:
                pass
            event.accept()

    win = ProxyManagerWindow()
    _open_windows.append(win)
    # BUILD 229: under the opener's menu bar (Footage Manager; no geometry
    # memory: every new window)
    from window_place import place_under_parent
    place_under_parent(win, parent, "proxy_manager")
    win.show()
    win.raise_()
    win.activateWindow()
    return win

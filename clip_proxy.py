# clip_proxy.py
# Proxy percent choice for footage (Footage Manager 'Proxy Default') and
# for clips (timeline 'Proxy' submenu). Values: 0 = None, else a percent
# that has a Proxies row for the footage, stored in footage.proxy_percent
# and clips.proxy_percent (new_project.py). ASCII only.

import os
import sqlite3
import subprocess
import sys

PROXY_PERCENTS = (100, 75, 50, 25, 10)   # BUILD 120: 100 (MKV proxies only)

# BUILD 120: several proxies may share a percent (Normal / Deinterlace /
# double rate / IVTC). The percent stays the choice; the composite uses the
# first mode of this order (same as sequence_player.PROXY_MODE_PREFERENCE)
# and the menus name it, e.g. '50% (deint)'.
MODE_PREFERENCE = ("deint", "ivtc", "deint2x", "normal")
MODE_TAGS = {"deint": "deint", "ivtc": "IVTC", "deint2x": "deint 2x"}


def proxy_label(pct, kind=None):
    if not pct:
        return "None"
    tag = MODE_TAGS.get(kind or "normal")
    return "%d%% (%s)" % (int(pct), tag) if tag else "%d%%" % int(pct)


def proxy_kinds(project_db, footage_id):
    # {pct: mode the composite uses at that percent}; {} on a read error
    try:
        conn = sqlite3.connect(project_db)
        try:
            rows = conn.execute(
                "SELECT percentage, COALESCE(proxy_mode, 'normal') FROM Proxies "
                "WHERE footage_id = ?", (int(footage_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for pct, mode in rows:
        rank = MODE_PREFERENCE.index(mode) if mode in MODE_PREFERENCE else 99
        cur = out.get(int(pct))
        if cur is None or rank < (MODE_PREFERENCE.index(cur) if cur in MODE_PREFERENCE else 99):
            out[int(pct)] = mode
    return out


def proxy_options(project_db, footage_id):
    # [0] + percents (100/75/50/25/10 order) with a Proxies row for footage_id
    have = set()
    try:
        conn = sqlite3.connect(project_db)
        try:
            for (p,) in conn.execute(
                    "SELECT percentage FROM Proxies WHERE footage_id = ?",
                    (int(footage_id),)).fetchall():
                have.add(int(p))
        finally:
            conn.close()
    except Exception:
        have = set()
    return [0] + [p for p in PROXY_PERCENTS if p in have]


def _read_percent(project_db, table, row_id):
    try:
        conn = sqlite3.connect(project_db)
        try:
            row = conn.execute(
                "SELECT COALESCE(proxy_percent, 0) FROM %s WHERE id = ?" % table,
                (int(row_id),)).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def footage_proxy_percent(project_db, footage_id):
    return _read_percent(project_db, "footage", footage_id)


def clip_proxy_percent(project_db, clip_id):
    return _read_percent(project_db, "clips", clip_id)


def _write_percent(project_db, table, row_id, pct, label):
    # Undoable (undo.py snapshot covers footage and clips). Returns True
    # once written (a failed write raises).
    try:
        from undo import push_undo
        push_undo(project_db, label)
    except Exception:
        pass
    conn = sqlite3.connect(project_db)
    try:
        conn.execute("UPDATE %s SET proxy_percent = ? WHERE id = ?" % table,
                     (int(pct), int(row_id)))
        conn.commit()
    finally:
        conn.close()
    return True


def set_footage_proxy_percent(project_db, footage_id, pct):
    return _write_percent(project_db, "footage", footage_id, pct, "Proxy default")


def set_clip_proxy_percent(project_db, clip_id, pct):
    return _write_percent(project_db, "clips", clip_id, pct, "Clip proxy")


def build_proxy_menu(menu, options, current, kinds=None):
    # Checkable actions on menu; returns {action: pct}. kinds: proxy_kinds
    out = {}
    for pct in options:
        act = menu.addAction(proxy_label(pct, (kinds or {}).get(int(pct))))
        act.setCheckable(True)
        act.setChecked(int(pct) == int(current or 0))
        out[act] = int(pct)
    return out


def add_clip_proxy_submenu(timeline, menu, clip):
    # 'Proxy' submenu for a video / image clip; returns {action: pct}
    db = timeline._project_db()
    sub = menu.addMenu("Proxy")
    return build_proxy_menu(sub, proxy_options(db, clip[1]),
                            clip_proxy_percent(db, clip[0]),
                            proxy_kinds(db, clip[1]))


def apply_clip_proxy(timeline, clip, pct):
    db = timeline._project_db()
    set_clip_proxy_percent(db, clip[0], pct)
    # The preview composite uses the clip's proxy: rebuild its frames
    try:
        timeline._mark_preview_dirty(timeline._ranges_for_clips([clip]))
    except Exception:
        pass
    timeline._after_clip_change("Clip proxy: %s" % proxy_label(pct))


def probe_video_sar(path):
    # BUILD 324: (width, height, sar_num, sar_den) of stream V:0 via
    # ffprobe, or None. A missing / "N/A" / "0:1" SAR reads as 1:1.
    flags = 0x08000000 if sys.platform.startswith("win") else 0   # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "V:0",
             "-show_entries", "stream=width,height,sample_aspect_ratio",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=20, creationflags=flags)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    vals = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    try:
        w = int(vals.get("width") or 0)
        h = int(vals.get("height") or 0)
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    num = den = 1
    sar = vals.get("sample_aspect_ratio") or ""
    if ":" in sar:
        try:
            n, d = sar.split(":", 1)
            n, d = int(n), int(d)
            if n > 0 and d > 0:
                num, den = n, d
        except ValueError:
            pass
    return w, h, num, den


_SAR_CACHE = {}   # (path, mtime, bytes) -> probe_video_sar result


def cached_video_sar(path):
    # BUILD 325: probe_video_sar once per file version (cached), for the
    # Footage Manager size columns and the icon generator; None when the
    # file is missing or unreadable
    try:
        st = os.stat(path)
        key = (path, st.st_mtime, st.st_size)
    except (OSError, TypeError, ValueError):
        return None
    if key not in _SAR_CACHE:
        _SAR_CACHE[key] = probe_video_sar(path)
    return _SAR_CACHE[key]


def format_pixel_aspect(sar_num, sar_den):
    # BUILD 326: "N:D" for footage.pixel_aspect (1:1, 8:9, 32:27, 4:3)
    return "%d:%d" % (int(sar_num), int(sar_den))


def parse_pixel_aspect(text):
    # BUILD 326: "N:D" -> (num, den), else None (NULL / empty / junk)
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    if ":" not in t:
        return None
    try:
        n, d = t.split(":", 1)
        n, d = int(n), int(d)
        if n > 0 and d > 0:
            return n, d
    except ValueError:
        pass
    return None


def read_footage_pixel_aspect(project_db, footage_id):
    # BUILD 326: (num, den) from footage.pixel_aspect, or None when the
    # column / row is missing or empty (old DB before migration, stills)
    if not project_db or footage_id is None:
        return None
    try:
        conn = sqlite3.connect(project_db)
        try:
            row = conn.execute(
                "SELECT pixel_aspect FROM footage WHERE id = ?",
                (int(footage_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    return parse_pixel_aspect(row[0])


def write_footage_pixel_aspect(project_db, footage_id, sar_num, sar_den):
    # BUILD 326: store "N:D" on footage.pixel_aspect; no-op when the
    # column is missing (pre-migration) or the write fails
    if not project_db or footage_id is None:
        return
    try:
        conn = sqlite3.connect(project_db)
        try:
            conn.execute(
                "UPDATE footage SET pixel_aspect = ? WHERE id = ?",
                (format_pixel_aspect(sar_num, sar_den), int(footage_id)))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def resolve_footage_sar(project_db, footage_id, path):
    # BUILD 326: (w, h, num, den) preferring footage.pixel_aspect + stored
    # width/height; ffprobe only when the DB value is missing, then write
    # it back. None for missing files / non-video / unreadable.
    if path and project_db and footage_id is not None:
        try:
            conn = sqlite3.connect(project_db)
            try:
                row = conn.execute(
                    "SELECT width, height, pixel_aspect FROM footage WHERE id = ?",
                    (int(footage_id),)).fetchone()
            finally:
                conn.close()
        except Exception:
            row = None
        if row is not None:
            parsed = parse_pixel_aspect(row[2])
            if parsed is not None and row[0] and row[1]:
                return int(row[0]), int(row[1]), parsed[0], parsed[1]
    info = cached_video_sar(path) if path else None
    if info is None:
        return None
    if project_db and footage_id is not None:
        write_footage_pixel_aspect(project_db, footage_id, info[2], info[3])
    return info


def display_width(width, sar_num, sar_den):
    # BUILD 324: square-pixel width of a frame with this SAR, rounded to
    # even (720x480 SAR 8:9 -> 640, 32:27 -> 854; 1440x1080 4:3 -> 1920)
    if not sar_num or not sar_den or sar_num == sar_den:
        return int(width)
    w = float(width) * sar_num / float(sar_den)
    return max(2, int(w / 2.0 + 0.5) * 2)


def probe_video_info(path):
    # (width, height, codec_name) of stream V:0 via ffprobe, or None.
    # BUILD 118: V (not v) skips attached pictures / cover art
    flags = 0x08000000 if sys.platform.startswith("win") else 0   # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "V:0",
             "-show_entries", "stream=width,height,codec_name",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=20, creationflags=flags)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    vals = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    try:
        w = int(vals.get("width") or 0) or None
        h = int(vals.get("height") or 0) or None
    except ValueError:
        w = h = None
    codec = vals.get("codec_name") or None
    if w is None and h is None and codec is None:
        return None
    return w, h, codec

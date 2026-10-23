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

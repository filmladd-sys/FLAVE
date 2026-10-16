# clip_proxy.py
# Proxy percent choice for footage (Footage Manager 'Proxy Default') and
# for clips (timeline 'Proxy' submenu). Values: 0 = None, else a percent
# that has a Proxies row for the footage. Columns footage.proxy_percent and
# clips.proxy_percent exist only in projects made by new_project.py BUILD 89+;
# older DBs read 0 and writes are refused. The preview does not use proxies.
# ASCII only.

import os
import sqlite3
import subprocess
import sys

PROXY_PERCENTS = (75, 50, 25, 10)


def proxy_label(pct):
    return "None" if not pct else "%d%%" % int(pct)


def proxy_options(project_db, footage_id):
    # [0] + percents (75/50/25/10 order) with a Proxies row for footage_id
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


def _has_column(project_db, table, column):
    try:
        conn = sqlite3.connect(project_db)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        finally:
            conn.close()
        return column in cols
    except Exception:
        return False


def _write_percent(project_db, table, row_id, pct, label):
    # Undoable (undo.py snapshot covers footage and clips). False if the
    # column is missing (older project).
    if not _has_column(project_db, table, "proxy_percent"):
        return False
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


def build_proxy_menu(menu, options, current):
    # Checkable actions on menu; returns {action: pct}
    out = {}
    for pct in options:
        act = menu.addAction(proxy_label(pct))
        act.setCheckable(True)
        act.setChecked(int(pct) == int(current or 0))
        out[act] = int(pct)
    return out


def add_clip_proxy_submenu(timeline, menu, clip):
    # 'Proxy' submenu for a video / image clip; returns {action: pct}
    db = timeline._project_db()
    sub = menu.addMenu("Proxy")
    return build_proxy_menu(sub, proxy_options(db, clip[1]),
                            clip_proxy_percent(db, clip[0]))


def apply_clip_proxy(timeline, clip, pct):
    db = timeline._project_db()
    if not set_clip_proxy_percent(db, clip[0], pct):
        timeline.status_bar.showMessage(
            "Proxy not available in this project (older version)", 3000)
        return
    # The preview composite uses the clip's proxy: rebuild its frames
    try:
        timeline._mark_preview_dirty(timeline._ranges_for_clips([clip]))
    except Exception:
        pass
    timeline._after_clip_change("Clip proxy: %s" % proxy_label(pct))


def probe_video_info(path):
    # (width, height, codec_name) of stream v:0 via ffprobe, or None
    flags = 0x08000000 if sys.platform.startswith("win") else 0   # CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
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

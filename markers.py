# markers.py
# Clip markers (Marker table, created only in new_project.py _init_db).
# Marker.clip_id is the timeline's in-memory clip id: clips.id for video /
# image / audio clips, -captions.id for caption clips (keyframe clips have
# none). Marker.frame is relative to the clip's start_frame (timeline
# frames), so a marker moves with its clip. Older DBs without Marker load
# no markers and refuse adds. ASCII only.

import sqlite3

DEFAULT_RGB = (139, 0, 0)
MARK_SIZE = 4


def load_markers(project_db):
    # {clip_id: [(marker_id, rel_frame, (r, g, b), name)]}; {} if no table
    out = {}
    if not project_db:
        return out
    try:
        conn = sqlite3.connect(project_db)
        try:
            rows = conn.execute(
                "SELECT id, clip_id, frame, r, g, b, COALESCE(name, '') FROM Marker "
                "ORDER BY frame, id").fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for mid, cid, frame, r, g, b, name in rows:
        out.setdefault(int(cid), []).append(
            (int(mid), int(frame), (int(r), int(g), int(b)), name))
    return out


def marker_frames(markers, clips_by_track, exclude_ids=()):
    # Absolute timeline frames of markers inside their clip's span
    out = set()
    if not markers:
        return []
    for clips in clips_by_track.values():
        for c in clips:
            if c[0] in exclude_ids:
                continue
            start, length = int(c[2]), max(1, int(c[3]))
            for _mid, rel, _rgb, _name in markers.get(c[0], ()):
                if 0 <= rel < length:
                    out.add(start + rel)
    return sorted(out)


def add_marker(project_db, clip, frame, name=""):
    # Insert at absolute timeline frame (clamped into the clip). Returns the
    # new id, or None when the Marker table is missing (older project).
    start, length = int(clip[2]), max(1, int(clip[3]))
    rel = max(0, min(length - 1, int(frame) - start))
    conn = sqlite3.connect(project_db)
    try:
        try:
            cur = conn.execute(
                "INSERT INTO Marker (name, clip_id, frame, r, g, b) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, int(clip[0]), rel) + DEFAULT_RGB)
        except sqlite3.OperationalError:
            return None
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def paint_markers(painter, clip_rect, clip, markers, ppf):
    # 4x4 square at the clip top plus a full-height line, marker color
    from PySide6.QtGui import QColor
    items = markers.get(clip[0]) if markers else None
    if not items or ppf <= 0:
        return
    length = max(1, int(clip[3]))
    for _mid, rel, rgb, _name in items:
        if not (0 <= rel < length):
            continue
        color = QColor(rgb[0], rgb[1], rgb[2])
        x = int(clip_rect.left() + rel * ppf)
        painter.setPen(color)
        painter.drawLine(x, clip_rect.top(), x, clip_rect.bottom())
        painter.fillRect(x - MARK_SIZE // 2, clip_rect.top(), MARK_SIZE, MARK_SIZE,
                         color)

# markers.py
# Clip markers (Marker table, created only in new_project.py _init_db).
# Marker.clip_id is the timeline's in-memory clip id: clips.id for video /
# image / audio clips, -captions.id for caption clips (keyframe clips have
# none). Marker.frame is in SOURCE terms (BUILD 95): clip in_frame + offset
# at add time, so trims and slices never move it on the picture. Timeline
# frame = clip start + (frame - clip in_frame); markers outside the clip's
# visible range are hidden. Captions have no in-point (in_frame 0 in the
# timeline tuple), so a caption marker is an offset from the caption start.
# Older DBs without Marker load no markers and refuse adds. ASCII only.

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


def _clip_in(clip):
    return int(clip[7]) if len(clip) > 7 and clip[7] is not None else 0


def visible_offset(clip, marker_frame):
    # Offset from the clip start, or None when outside the visible range
    off = int(marker_frame) - _clip_in(clip)
    return off if 0 <= off < max(1, int(clip[3])) else None


def marker_frames(markers, clips_by_track, exclude_ids=()):
    # Absolute timeline frames of visible markers
    out = set()
    if not markers:
        return []
    for clips in clips_by_track.values():
        for c in clips:
            if c[0] in exclude_ids:
                continue
            for _mid, mframe, _rgb, _name in markers.get(c[0], ()):
                off = visible_offset(c, mframe)
                if off is not None:
                    out.add(int(c[2]) + off)
    return sorted(out)


def delete_markers(cur, mem_ids):
    # Marker rows of deleted clips (caller's transaction / undo snapshot)
    ids = [int(i) for i in mem_ids]
    if not ids:
        return
    try:
        cur.execute("DELETE FROM Marker WHERE clip_id IN (%s)" %
                    ",".join("?" for _ in ids), ids)
    except sqlite3.OperationalError:
        pass


def move_markers(cur, old_mem, new_mem, lo, hi, delta=0):
    # Slice: markers with lo <= frame < hi go to new_mem (frame += delta)
    try:
        cur.execute(
            "UPDATE Marker SET clip_id = ?, frame = frame + ? "
            "WHERE clip_id = ? AND frame >= ? AND frame < ?",
            (int(new_mem), int(delta), int(old_mem), int(lo), int(hi)))
    except sqlite3.OperationalError:
        pass


def marker_rows(cur, mem_id):
    # [(name, frame, r, g, b)] for copy / paste
    try:
        return cur.execute(
            "SELECT name, frame, r, g, b FROM Marker WHERE clip_id = ? ORDER BY id",
            (int(mem_id),)).fetchall()
    except sqlite3.OperationalError:
        return []


def insert_marker_rows(cur, mem_id, rows):
    for name, frame, r, g, b in rows or ():
        try:
            cur.execute(
                "INSERT INTO Marker (name, clip_id, frame, r, g, b) "
                "VALUES (?, ?, ?, ?, ?, ?)", (name, int(mem_id), frame, r, g, b))
        except sqlite3.OperationalError:
            return


def add_marker(project_db, clip, frame, name=""):
    # Insert at absolute timeline frame (clamped into the clip). Returns the
    # new id, or None when the Marker table is missing (older project).
    start, length = int(clip[2]), max(1, int(clip[3]))
    rel = max(0, min(length - 1, int(frame) - start))
    src = _clip_in(clip) + rel
    conn = sqlite3.connect(project_db)
    try:
        try:
            cur = conn.execute(
                "INSERT INTO Marker (name, clip_id, frame, r, g, b) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, int(clip[0]), src) + DEFAULT_RGB)
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
    for _mid, mframe, rgb, _name in items:
        rel = visible_offset(clip, mframe)
        if rel is None:
            continue
        color = QColor(rgb[0], rgb[1], rgb[2])
        x = int(clip_rect.left() + rel * ppf)
        painter.setPen(color)
        painter.drawLine(x, clip_rect.top(), x, clip_rect.bottom())
        painter.fillRect(x - MARK_SIZE // 2, clip_rect.top(), MARK_SIZE, MARK_SIZE,
                         color)

# sequence_timeline_droppreview.py
# Drop preview boxes while footage from the Footage Manager is
# dragged over the Sequence Timeline. Preview only: drop_footage
# (sequence_timeline_sql.py) still decides everything. The math mirrors it:
#   row = table.rowAt(y); x_in_clips = x - Clips column position
#   start = max(0, view_start_frame + int(x_in_clips / pixels_per_frame))
#   (no snapping in drop_footage, so none here)
#   image footage -> image tracks only; video -> video; audio -> audio
# video length: footage_length_frames (footage.duration
#     else peaks, else 1 s) cut to the room before the next clip by
#     fit_video_drop; a linked audio clip (peaks exist and the sequence has
#     an audio track) goes on _nearest_free_track, below first, then above,
#     and the room on its track cuts the length too
#   audio length: _audio_length_frames (peaks, else footage.duration, 1 s)
#   refused (red): video drop frame inside a clip on its track, or no
#     audio track free there; image / audio drops have no overlap check in
#     drop_footage and are never shown red
# Footage facts are read once per drag (dragEnter); each move uses the
# table rows and clips_by_track already in memory (no database query).
# ASCII only.

import os
import sqlite3

from PySide6.QtCore import Qt, QRect


def _clips_col():
    from sequence_timeline_ruler import CLIPS_COL
    return CLIPS_COL


def footage_drag_info(tl, footage_id):
    # One query pass at dragEnter: {"footage_id", "accepts": {track_type},
    # "video_len", "audio_len", "linked_audio"} or None
    from sequence_timeline_sql import TRACK_FOOTAGE_EXTENSIONS
    try:
        project_db = tl._project_db()
        if not project_db or not os.path.isfile(project_db):
            return None
        conn = sqlite3.connect(project_db)
        try:
            cur = conn.cursor()
            cur.execute("SELECT filename, original_path FROM footage WHERE id = ?",
                        (int(footage_id),))
            frow = cur.fetchone()
            if not frow:
                return None
            accepts = set()
            for ttype, wanted in TRACK_FOOTAGE_EXTENSIONS.items():
                if any(v and str(v).lower().endswith(wanted) for v in frow):
                    accepts.add(ttype)
            fps = tl.fps
            video_len = max(1, int(round(fps)))
            linked = False
            if "video" in accepts:
                from sequence_timeline_sql import footage_length_frames
                peak_seconds = tl._peaks_seconds(cur, int(footage_id))
                video_len = footage_length_frames(cur, int(footage_id), fps,
                                                  peak_seconds)
                linked = (peak_seconds > 0
                          and tl._first_audio_track_id(cur) is not None)
            audio_len = (tl._audio_length_frames(cur, int(footage_id))
                         if "audio" in accepts else None)
        finally:
            conn.close()
    except Exception:
        return None
    return {"footage_id": int(footage_id), "accepts": accepts,
            "video_len": video_len, "audio_len": audio_len, "linked_audio": linked}


def track_rows(tl):
    # [(row, track_id, track_type)] in table order (= ORDER BY sort_order, id)
    out = []
    for r in range(tl.table.rowCount()):
        item = tl.table.item(r, 0)
        if item is None:
            continue
        tid = item.data(Qt.UserRole)
        if tid is None:
            continue
        out.append((r, int(tid), (item.data(Qt.UserRole + 4) or "").lower()))
    return out


class _MemCursor(object):
    # Answers _nearest_free_track's one tracks query from the table rows
    def __init__(self, rows):
        self._rows = [(tid, ttype) for _r, tid, ttype in rows]

    def execute(self, sql, params=()):
        return self

    def fetchall(self):
        return list(self._rows)


class _MemTimeline(object):
    # sequence_id + _range_overlaps on clips_by_track (in memory), so the
    # drop's own _nearest_free_track runs unchanged without the database
    def __init__(self, tl):
        self.sequence_id = tl.sequence_id
        self._clips = tl.clips_by_track

    def _range_overlaps(self, cur, track_id, start, length):
        return mem_range_overlaps(self._clips, track_id, start, length)


def mem_next_room(clips_by_track, track_id, start):
    # Same as next_clip_room: frames to the next clip at or after start
    nxt = [c[2] for c in clips_by_track.get(track_id, []) if c[2] >= start]
    return min(nxt) - start if nxt else None


def mem_range_overlaps(clips_by_track, track_id, start, length):
    # Same test as _range_overlaps: [start, start + length) vs each clip
    for c in clips_by_track.get(track_id, []):
        if c[2] < start + length and c[2] + c[3] > start:
            return True
    return False


def preview_boxes(tl, info, pos):
    # [(row, start_frame, length_frames, refused)] for the drop at pos
    # (viewport point), [] where drop_footage would not add a clip
    if not info:
        return []
    row = tl.table.rowAt(pos.y())
    x_in_clips = pos.x() - tl.ruler.sectionViewportPosition(_clips_col())
    if row < 0 or x_in_clips < 0:
        return []
    rows = track_rows(tl)
    by_row = dict((r, (tid, tt)) for r, tid, tt in rows)
    if row not in by_row:
        return []
    track_id, track_type = by_row[row]
    if track_type not in ("image", "video", "audio") or track_type not in info["accepts"]:
        return []
    start = max(0, tl.view_start_frame + int(x_in_clips / tl.pixels_per_frame))
    if track_type == "image":
        return [(row, start, max(1, int(round(tl.fps))), False)]
    if track_type == "audio":
        return [(row, start, info["audio_len"] or max(1, int(round(tl.fps))), False)]
    from sequence_timeline_sql import fit_video_drop
    clips = tl.clips_by_track
    length, tid, why = fit_video_drop(
        _MemTimeline(tl), _MemCursor(rows), track_id, start, info["video_len"],
        info["linked_audio"], lambda t, st: mem_next_room(clips, t, st))
    boxes = []
    audio_row = None
    if tid is not None:
        audio_row = [r for r, t, _tt in rows if t == tid][0]
    refused = why is not None
    boxes.append((row, start, length, refused))
    if audio_row is not None:
        boxes.append((audio_row, start, length, refused))
    return boxes


def box_rect(tl, box):
    # Viewport QRect of one box, clipped to the Clips column; None if hidden
    row, start, length, _refused = box
    table = tl.table
    left = table.columnViewportPosition(_clips_col())
    right = left + table.columnWidth(_clips_col())
    x0 = left + int((start - tl.view_start_frame) * tl.pixels_per_frame)
    x1 = left + int((start + length - tl.view_start_frame) * tl.pixels_per_frame)
    x0, x1 = max(x0, left), min(x1, right)
    if x1 <= x0:
        return None
    y = table.rowViewportPosition(row)
    h = table.rowHeight(row)
    return QRect(x0, y, x1 - x0, max(1, h - 1))


def _boxes_region(tl, boxes):
    rect = QRect()
    for b in boxes or []:
        r = box_rect(tl, b)
        if r is not None:
            rect = rect.united(r.adjusted(-2, -2, 2, 2))
    return rect


def set_preview(tl, boxes):
    # Store boxes and repaint only the old and new box areas
    old = getattr(tl, "_drop_preview", None) or []
    boxes = boxes or []
    if old == boxes:
        return
    area = _boxes_region(tl, old).united(_boxes_region(tl, boxes))
    tl._drop_preview = boxes
    overlay = getattr(tl, "playhead_overlay", None)
    if overlay is not None and not area.isNull():
        overlay.update(area)


def drag_enter(tl, footage_id, pos):
    tl._drop_info = footage_drag_info(tl, footage_id)
    set_preview(tl, preview_boxes(tl, tl._drop_info, pos))


def drag_move(tl, pos):
    set_preview(tl, preview_boxes(tl, getattr(tl, "_drop_info", None), pos))


def drag_end(tl):
    # dragLeave (also Esc cancel), drop, or a drag this preview ignores
    tl._drop_info = None
    set_preview(tl, [])


def paint_preview(tl, painter):
    # Called from PlayheadOverlay.paintEvent (on top of the clips)
    from PySide6.QtGui import QColor, QPen
    for b in getattr(tl, "_drop_preview", None) or []:
        r = box_rect(tl, b)
        if r is None:
            continue
        if b[3]:
            fill, edge = QColor(255, 60, 60, 60), QColor(255, 80, 80)
        else:
            fill, edge = QColor(255, 255, 255, 40), QColor(255, 255, 255)
        painter.fillRect(r, fill)
        pen = QPen(edge)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(r.adjusted(0, 0, -1, -1))

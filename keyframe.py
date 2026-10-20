# keyframe.py
# Keyframe clip tables, dialog, load/draw/persist helpers. ASCII only.

import sqlite3

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QMessageBox, QDoubleSpinBox, QApplication, QInputDialog,
)

# Stored on keyframe_clips.type. BUILD 147: one type per keyframe clip;
# it can change only while the clip has no keys (add_key, set_clip_type).
# KEYFRAME_CLIP_TYPES = ("position X", "position Y", "scale", "volume", "opacity")
# BUILD 147: crop_x / crop_y (source pixels, default 0, CUDA blend only)
# KEYFRAME_CLIP_TYPES = ("position X", "position Y", "scale", "volume", "opacity",
#                        "crop_x", "crop_y")
# BUILD 154: gamma / brightness / contrast (color, CUDA blend only)
KEYFRAME_CLIP_TYPES = ("position X", "position Y", "scale", "volume", "opacity",
                       "crop_x", "crop_y", "gamma", "brightness", "contrast")

# Combo display label -> stored type
KEYFRAME_TYPE_CHOICES = (
    ("X", "position X"),
    ("Y", "position Y"),
    ("Scale", "scale"),
    ("Volume", "volume"),
    ("Opacity", "opacity"),
    ("X Crop", "crop_x"),     # BUILD 147
    ("Y Crop", "crop_y"),     # BUILD 147
    ("Gamma", "gamma"),               # BUILD 154
    ("Brightness", "brightness"),     # BUILD 154
    ("Contrast", "contrast"),         # BUILD 154
)

# Value range per stored type (others unbounded). Volume and opacity are
# percent 0..100.
# KEY_VALUE_RANGES = {"volume": (0.0, 100.0), "opacity": (0.0, 100.0)}
# BUILD 154: gamma 0.1..5 (1 = none), brightness percent -100..100 (0 =
# none), contrast 0..5 (1 = none); the blend clamps the same way
KEY_VALUE_RANGES = {"volume": (0.0, 100.0), "opacity": (0.0, 100.0),
                    "gamma": (0.1, 5.0), "brightness": (-100.0, 100.0),
                    "contrast": (0.0, 5.0)}


def clamp_key_value(stored_type, value):
    # Clamp a key value to its type's range (no-op for unbounded types)
    rng = KEY_VALUE_RANGES.get(stored_type or "")
    value = float(value)
    if rng is None:
        return value
    return max(rng[0], min(rng[1], value))

# In-memory clip ids: -(KEYFRAME_ID_BASE + db_id) so they never collide
# with caption negatives (-captions.id) or positive clips.id.
KEYFRAME_ID_BASE = 1000000000

TITLE_BAR_H = 15
KEY_MARK_SIZE = 20


def ensure_keyframe_clips_table(conn):
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


def ensure_keyframes_table(conn):
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


def ensure_keyframe_tables(conn):
    # Schema lives in new_project.py (BUILD 78); kept as a no-op for callers
    return None


def is_keyframe_clip_id(clip_id):
    return clip_id is not None and int(clip_id) <= -KEYFRAME_ID_BASE


def mem_id_from_db(db_id):
    return -(KEYFRAME_ID_BASE + int(db_id))


def db_id_from_mem(clip_id):
    return (-int(clip_id)) - KEYFRAME_ID_BASE


def type_display_label(stored_type):
    for label, stored in KEYFRAME_TYPE_CHOICES:
        if stored == stored_type:
            return label
    return stored_type or ""


def clip_title(name, stored_type):
    return "%s: %s" % ((name or ""), type_display_label(stored_type))


def load_keyframe_clips(cur, sequence_id, clips):
    # Into clips_by_track like captions: list
    # [mem_id, None, start, length, title, enabled, solo, in_src, link_id,
    #  keys, type] where keys is [(key_id, absolute_frame, value), ...]
    by_db = {}
    try:
        cur.execute(
            "SELECT id, track_id, type, link_id, in_frame, out_frame, "
            "COALESCE(name, '') FROM keyframe_clips "
            "WHERE sequence_id = ? ORDER BY in_frame, id",
            (sequence_id,)
        )
    except sqlite3.OperationalError:
        return
    for row in cur.fetchall():
        db_id, track_id, ktype, link_id, in_f, out_f, name = row
        in_f = max(0, int(in_f or 0))
        out_f = max(in_f + 1, int(out_f or (in_f + 1)))
        length = max(1, out_f - in_f)
        entry = [mem_id_from_db(db_id), None, in_f, length,
                 clip_title(name, ktype), 1, 0, 0, link_id, [], ktype]
        clips.setdefault(track_id, []).append(entry)
        by_db[int(db_id)] = entry
    if not by_db:
        return
    try:
        cur.execute(
            "SELECT k.id, k.keyframe_clip_id, k.frame, k.value FROM keyframes k "
            "JOIN keyframe_clips c ON c.id = k.keyframe_clip_id "
            "WHERE c.sequence_id = ? ORDER BY k.frame, k.id",
            (sequence_id,)
        )
    except sqlite3.OperationalError:
        return
    for key_id, db_id, frame, value in cur.fetchall():
        entry = by_db.get(int(db_id))
        if entry is not None:
            entry[9].append((int(key_id), int(frame), float(value)))


def format_key_value(value, stored_type):
    # Position X/Y, scale and volume: two decimal places
    return "%.2f" % float(value)


def evaluate_keyframes(keys, frame):
    # Pure: keys are (key_id, frame, value) or (frame, value). One key =
    # that value for the whole clip; two or more = linear between keys,
    # hold flat before the first and after the last. No keys = None.
    if not keys:
        return None
    points = []
    for item in keys:
        if item is None:
            continue
        if len(item) >= 3:
            points.append((int(item[1]), float(item[2])))
        elif len(item) >= 2:
            points.append((int(item[0]), float(item[1])))
    if not points:
        return None
    points.sort(key=lambda p: (p[0], p[1]))
    f = int(frame)
    if len(points) == 1 or f <= points[0][0]:
        return points[0][1]
    if f >= points[-1][0]:
        return points[-1][1]
    for i in range(len(points) - 1):
        f0, v0 = points[i]
        f1, v1 = points[i + 1]
        if f0 <= f <= f1:
            if f1 == f0:
                return v1
            t = (f - f0) / float(f1 - f0)
            return v0 + (v1 - v0) * t
    return points[-1][1]


def evaluate_scale(keys, frame):
    # Scale key value (percent), or None when there are no keys.
    return evaluate_keyframes(keys, frame)


def _pick_above_keyframe(clips, clip_link_id, frame, clip_order):
    # Among linked keyframe clips covering frame whose track sits above
    # clip_order (smaller table-row / sort_order index = higher on the
    # timeline), pick the nearest above. clips entries end with order.
    if clip_link_id is None or not clips or clip_order is None:
        return None
    best_keys = None
    best_order = None
    for link_id, in_f, out_f, keys, kf_order in clips:
        if link_id is None or link_id != clip_link_id:
            continue
        if kf_order is None or int(kf_order) >= int(clip_order):
            continue
        if not (int(in_f) <= int(frame) < int(out_f)):
            continue
        if best_order is None or int(kf_order) > int(best_order):
            best_order = int(kf_order)
            best_keys = keys
    return best_keys


def scale_percent_for_link(scale_clips, clip_link_id, frame, clip_order,
                           base_scale=100.0):
    # Linked type-scale keyframe above this clip (same link_id, covers
    # frame). base_scale is the clip's existing scale percent (video/image
    # have no scale column; callers pass 100). Returns final percent =
    # base * value/100, or None when there is no effect.
    keys = _pick_above_keyframe(scale_clips, clip_link_id, frame, clip_order)
    if keys is None:
        return None
    value = evaluate_scale(keys, frame)
    if value is None:
        return None
    return float(base_scale) * float(value) / 100.0


def position_x_offset(pos_x_clips, clip_link_id, frame, clip_order, seq_w):
    # Absolute center X in sequence pixels; offset = value - seq_w/2.
    # Default Add Key (seq_w/2) means no shift. None when no effect.
    keys = _pick_above_keyframe(pos_x_clips, clip_link_id, frame, clip_order)
    if keys is None:
        return None
    value = evaluate_keyframes(keys, frame)
    if value is None:
        return None
    return float(value) - float(seq_w) / 2.0


def position_y_offset(pos_y_clips, clip_link_id, frame, clip_order, seq_h):
    # Absolute center Y in sequence pixels; offset = value - seq_h/2, same
    # rule as position_x_offset. Default Add Key (seq_h/2) means no shift.
    keys = _pick_above_keyframe(pos_y_clips, clip_link_id, frame, clip_order)
    if keys is None:
        return None
    value = evaluate_keyframes(keys, frame)
    if value is None:
        return None
    return float(value) - float(seq_h) / 2.0


def opacity_for_link(opacity_clips, clip_link_id, frame, clip_order):
    # Linked type-opacity keyframe above this clip (same link_id, covers
    # frame): alpha multiplier value/100 clamped 0..1, or None (= 1.0).
    keys = _pick_above_keyframe(opacity_clips, clip_link_id, frame, clip_order)
    if keys is None:
        return None
    value = evaluate_keyframes(keys, frame)
    if value is None:
        return None
    return max(0.0, min(100.0, float(value))) / 100.0


def crop_offset_for_link(crop_clips, clip_link_id, frame, clip_order):
    # BUILD 147: linked type-crop_x / crop_y keyframe above this clip (same
    # rule as scale / position): the crop offset in source pixels of the
    # original footage (0 = none), or None when there is no effect.
    keys = _pick_above_keyframe(crop_clips, clip_link_id, frame, clip_order)
    if keys is None:
        return None
    value = evaluate_keyframes(keys, frame)
    if value is None:
        return None
    return float(value)


def color_value_for_link(color_clips, clip_link_id, frame, clip_order):
    # BUILD 154: linked type-gamma / brightness / contrast keyframe above
    # this clip (same rule as scale / position / crop): the raw key value
    # (linear, flat outside the first / last key), or None = no effect.
    keys = _pick_above_keyframe(color_clips, clip_link_id, frame, clip_order)
    if keys is None:
        return None
    value = evaluate_keyframes(keys, frame)
    if value is None:
        return None
    return float(value)


def _collect_keyframe_clips_of_type(clips_by_track, track_order, stored_type):
    # Once per snapshot: (link_id, in_frame, out_frame, keys, order) for
    # linked keyframe clips of stored_type. order is table row index
    # (0 = top of timeline = smaller sort_order).
    out = []
    if not clips_by_track:
        return out
    for track_id, track_clips in clips_by_track.items():
        order = None if track_order is None else track_order.get(track_id)
        if order is None:
            continue
        for c in track_clips:
            if not is_keyframe_clip_id(c[0]):
                continue
            if len(c) <= 10 or c[10] != stored_type:
                continue
            link_id = c[8] if len(c) > 8 else None
            if link_id is None:
                continue
            in_f = max(0, int(c[2]))
            out_f = in_f + max(1, int(c[3]))
            keys = list(c[9]) if len(c) > 9 and c[9] else []
            out.append((link_id, in_f, out_f, keys, int(order)))
    return out


def collect_scale_clips(clips_by_track, track_order=None):
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "scale")


def collect_position_x_clips(clips_by_track, track_order=None):
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "position X")


def collect_position_y_clips(clips_by_track, track_order=None):
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "position Y")


def collect_opacity_clips(clips_by_track, track_order=None):
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "opacity")


def collect_crop_x_clips(clips_by_track, track_order=None):
    # BUILD 147
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "crop_x")


def collect_crop_y_clips(clips_by_track, track_order=None):
    # BUILD 147
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "crop_y")


def collect_gamma_clips(clips_by_track, track_order=None):
    # BUILD 154
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "gamma")


def collect_brightness_clips(clips_by_track, track_order=None):
    # BUILD 154
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "brightness")


def collect_contrast_clips(clips_by_track, track_order=None):
    # BUILD 154
    return _collect_keyframe_clips_of_type(
        clips_by_track, track_order, "contrast")


def mark_keyframe_clip_preview_dirty(timeline, clip):
    # BUILD 139: no mark here. The edit's refresh diffs the timeline
    # snapshot (sequence_preview_diff), which includes every linked
    # keyframe clip's keys, so only the frames whose key pair / value
    # changed go dirty (between the neighbour keys of the edited key), not
    # the keyframe clip's whole [in, out). Kept for its callers.
    return



def key_square_size(text, font_metrics):
    pad = 4
    tw = font_metrics.horizontalAdvance(text)
    th = font_metrics.height()
    return max(KEY_MARK_SIZE, tw + pad * 2), max(KEY_MARK_SIZE, th + pad * 2)


def key_mark_layout(clip_rect, frame, value, clip_start, ppf, stored_type,
                    font_metrics):
    # (text, square QRect, center_x) for one key; line is full body height
    text = format_key_value(value, stored_type)
    w, h = key_square_size(text, font_metrics)
    title_h = min(TITLE_BAR_H, max(1, clip_rect.height()))
    body_top = clip_rect.top() + title_h
    body_h = max(0, clip_rect.height() - title_h)
    x = int(clip_rect.left() + (int(frame) - int(clip_start)) * ppf)
    cy = body_top + body_h // 2
    sq = QRect(x - w // 2, cy - h // 2, w, h)
    return text, sq, x, body_top


NOT_LINKED_SUFFIX = " (not linked yet)"


def title_with_link_state(title, linked):
    return (title or "") + ("" if linked else NOT_LINKED_SUFFIX)


def graph_value_range(stored_type, values):
    # (low, high) for the value graph: 0..100 for percent types, else min..max
    rng = KEY_VALUE_RANGES.get(stored_type or "")
    if rng is not None:
        return float(rng[0]), float(rng[1])
    return float(min(values)), float(max(values))


def graph_points(keys, stored_type, clip_start, length, ppf, left, top, height):
    # Polyline points [(x, y)] for the key value graph, or [] when there are
    # fewer than two keys or all values are equal. Flat before the first and
    # after the last key (clip in..out).
    pts = []
    for item in keys or []:
        if len(item) >= 3:
            pts.append((int(item[1]), float(item[2])))
        elif len(item) >= 2:
            pts.append((int(item[0]), float(item[1])))
    if len(pts) < 2:
        return []
    pts.sort(key=lambda p: (p[0], p[1]))
    values = [v for _f, v in pts]
    if max(values) == min(values):
        return []
    low, high = graph_value_range(stored_type, values)
    span = (high - low) or 1.0
    bottom = top + max(1, height) - 1

    def xy(frame, value):
        v = max(low, min(high, value))
        x = left + (frame - int(clip_start)) * ppf
        y = bottom - (v - low) / span * (max(1, height) - 1)
        return int(round(x)), int(round(y))

    # Keys may lie outside the clip after an edge trim (kept, hidden): the
    # flat lead-in / tail is added only where the first / last key is
    # inside the clip; the painter clips the rest to the clip rect
    end = int(clip_start) + int(length)
    out = []
    if pts[0][0] > int(clip_start):
        out.append(xy(int(clip_start), pts[0][1]))
    out += [xy(f, v) for f, v in pts]
    if pts[-1][0] < end:
        out.append(xy(end, pts[-1][1]))
    return out


def sticky_name_rect(text_rect, visible_rect=None, pad=4):
    # BUILD 127: clip names are left-aligned with a small padding and stick
    # to the visible left edge of the Clips area when the clip starts off
    # screen; the right side stops at the clip (or visible) right edge.
    # None if no room.
    from PySide6.QtCore import QRect
    left = text_rect.left()
    right = text_rect.right()
    if visible_rect is not None:
        left = max(left, visible_rect.left())
        right = min(right, visible_rect.right())
    left += pad
    right -= 2
    if right - left + 1 <= 0:
        return None
    return QRect(left, text_rect.top(), right - left + 1, text_rect.height())


TITLE_MIN_CONTENT = 8   # BUILD 128: rows too short for bar + content: no bar


def title_heights(clip_h):
    # BUILD 128: (title bar height, content height) for an open video /
    # audio / image / caption / sequence clip of height clip_h. Rows shorter
    # than TITLE_BAR_H + TITLE_MIN_CONTENT get no bar (0, clip_h).
    clip_h = int(clip_h)
    if clip_h < TITLE_BAR_H + TITLE_MIN_CONTENT:
        return 0, clip_h
    return TITLE_BAR_H, clip_h - TITLE_BAR_H


def split_title_rect(clip_rect):
    # (title_rect or None, content_rect) per title_heights
    th, ch = title_heights(clip_rect.height())
    if th <= 0:
        return None, clip_rect
    title_rect = clip_rect.adjusted(0, 0, 0, -ch)
    content = clip_rect.adjusted(0, th, 0, 0)
    return title_rect, content


def paint_title_bar(painter, title_rect, title, visible_rect=None):
    # Black bar, 1 px white border, white name: left-aligned, sticky to the
    # visible left edge, elided at the right (BUILD 127). Shared by keyframe
    # clips and (BUILD 128) the other open-track clip types.
    painter.fillRect(title_rect, QColor(0, 0, 0))
    painter.setPen(QPen(QColor(255, 255, 255)))
    painter.drawRect(title_rect.adjusted(0, 0, -1, -1))
    name_rect = sticky_name_rect(title_rect, visible_rect, 3)
    if name_rect is not None and name_rect.width() > 2:
        text = painter.fontMetrics().elidedText(
            " ".join((title or "").split()), Qt.ElideRight, name_rect.width())
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter, text)


def paint_keyframe_clip(painter, clip_rect, title, keys=None,
                        clip_start=0, ppf=1.0, stored_type="", linked=True,
                        visible_rect=None):
    # Caption-like body fill, plus a 15 px title bar. Keys: full-height white
    # vertical line, then a sized white square with black border and light
    # grey value text, clipped to the clip rect.
    body = QColor(30, 110, 110)
    painter.fillRect(clip_rect, body)
    h = min(TITLE_BAR_H, max(1, clip_rect.height()))
    title_rect = clip_rect.adjusted(0, 0, 0, -(clip_rect.height() - h))
    # BUILD 127 / 128: shared title bar painter (unchanged look)
    paint_title_bar(painter, title_rect, title_with_link_state(title, linked),
                    visible_rect)
    if not keys or ppf <= 0:
        return
    body_h = max(0, clip_rect.height() - h)
    if body_h <= 0:
        return
    painter.save()
    painter.setClipRect(clip_rect)
    # Value graph under the key marks (two or more keys, values differ)
    length = int(round(clip_rect.width() / float(ppf))) if ppf > 0 else 0
    gpts = graph_points(keys, stored_type, clip_start, length, ppf,
                        clip_rect.left(), clip_rect.top() + h, body_h)
    if gpts:
        painter.setPen(QPen(QColor(255, 210, 90), 2))
        for i in range(len(gpts) - 1):
            painter.drawLine(gpts[i][0], gpts[i][1], gpts[i + 1][0], gpts[i + 1][1])
    fm = painter.fontMetrics()
    for item in keys:
        if len(item) >= 3:
            _kid, frame, value = item[0], item[1], item[2]
        else:
            frame, value = item[0], item[1]
        text, sq, x, body_top = key_mark_layout(
            clip_rect, frame, value, clip_start, ppf, stored_type, fm)
        painter.setPen(QPen(QColor(255, 255, 255)))
        painter.drawLine(x, body_top, x, clip_rect.top() + clip_rect.height() - 1)
        painter.fillRect(sq, QColor(255, 255, 255))
        painter.setPen(QPen(QColor(0, 0, 0)))
        painter.drawRect(sq.adjusted(0, 0, -1, -1))
        painter.setPen(QPen(QColor(180, 180, 180)))
        painter.drawText(sq, Qt.AlignCenter, text)
    painter.restore()



def keyframe_range_overlaps(cur, track_id, start, length, ignore_id=None):
    # [start, start+length) vs [in_frame, out_frame)
    end = int(start) + int(length)
    sql = (
        "SELECT 1 FROM keyframe_clips WHERE track_id = ? "
        "AND in_frame < ? AND out_frame > ?"
    )
    args = [int(track_id), end, int(start)]
    if ignore_id is not None:
        sql += " AND id != ?"
        args.append(int(ignore_id))
    sql += " LIMIT 1"
    cur.execute(sql, args)
    return cur.fetchone() is not None


def persist_keyframe_clip_time(cur, clip, shift_keys=True):
    # Memory start/length -> in_frame/out_frame. Keys store absolute
    # sequence frames. shift_keys True (a move of the whole clip: body
    # drag, ripple): keys shift by the start delta so they stay with the
    # clip. shift_keys False (an edge trim): keys keep their frames, so the
    # trim only reveals or hides keys and they stay pinned to the media
    # they are linked with. Keys outside [in, out) are kept, not deleted:
    # extending the clip again shows them, and they still shape the
    # interpolated curve inside the clip exactly as if they were visible.
    db_id = db_id_from_mem(clip[0])
    in_f = max(0, int(clip[2]))
    length = max(1, int(clip[3]))
    cur.execute("SELECT in_frame FROM keyframe_clips WHERE id = ?", (db_id,))
    row = cur.fetchone()
    old_in = int(row[0]) if row is not None else in_f
    delta = in_f - old_in
    cur.execute(
        "UPDATE keyframe_clips SET in_frame = ?, out_frame = ? WHERE id = ?",
        (in_f, in_f + length, db_id)
    )
    if delta != 0 and shift_keys:
        cur.execute(
            "UPDATE keyframes SET frame = frame + ? WHERE keyframe_clip_id = ?",
            (delta, db_id)
        )
        if len(clip) > 9 and clip[9]:
            clip[9][:] = [
                (item[0], int(item[1]) + delta, item[2]) if len(item) >= 3
                else (int(item[0]) + delta, item[1])
                for item in clip[9]
            ]


def slice_keyframe_clip_db(cur, clip_mem_id, split):
    # BUILD 132: split one keyframe clip at absolute sequence frame split
    # (in_frame < split < out_frame). The clip keeps [in, split) and the
    # keys before split; a new keyframe_clips row (same sequence, track,
    # type, name and link_id) gets [split, out) and the keys at/after
    # split. Keys store absolute frames, so nothing is re-based. A boundary
    # key is added on each side holding the curve value there (left at
    # split - 1, right at split) unless a key already sits on that frame,
    # so evaluate_keyframes gives the same value on every frame as before.
    # No commit / undo here (the caller owns the transaction and undo step).
    # Returns the new piece's mem id, or None when not sliceable.
    db_id = db_id_from_mem(clip_mem_id)
    cur.execute(
        "SELECT sequence_id, track_id, type, link_id, in_frame, out_frame, name "
        "FROM keyframe_clips WHERE id = ?", (db_id,))
    row = cur.fetchone()
    if row is None:
        return None
    seq_id, track_id, ktype, link_id, in_f, out_f, name = row
    split = int(split)
    if not (int(in_f) < split < int(out_f)):
        return None
    cur.execute(
        "SELECT id, frame, value FROM keyframes WHERE keyframe_clip_id = ? "
        "ORDER BY frame, id", (db_id,))
    keys = [(int(k), int(f), float(v)) for k, f, v in cur.fetchall()]
    cur.execute(
        "INSERT INTO keyframe_clips "
        "(sequence_id, track_id, type, link_id, in_frame, out_frame, name) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (seq_id, track_id, ktype, link_id, split, int(out_f), name))
    new_db = cur.lastrowid
    cur.execute("UPDATE keyframe_clips SET out_frame = ? WHERE id = ?",
                (split, db_id))
    if keys:
        left_val = evaluate_keyframes(keys, split - 1)
        right_val = evaluate_keyframes(keys, split)
        frames = set(k[1] for k in keys)
        cur.execute(
            "UPDATE keyframes SET keyframe_clip_id = ? "
            "WHERE keyframe_clip_id = ? AND frame >= ?",
            (new_db, db_id, split))
        if (split - 1) not in frames:
            cur.execute(
                "INSERT INTO keyframes (keyframe_clip_id, frame, value) "
                "VALUES (?, ?, ?)", (db_id, split - 1, left_val))
        if split not in frames:
            cur.execute(
                "INSERT INTO keyframes (keyframe_clip_id, frame, value) "
                "VALUES (?, ?, ?)", (new_db, split, right_val))
    return mem_id_from_db(new_db)


def persist_keyframe_clip_track(cur, clip, track_id):
    db_id = db_id_from_mem(clip[0])
    cur.execute(
        "UPDATE keyframe_clips SET track_id = ? WHERE id = ?",
        (int(track_id), db_id)
    )


def persist_keyframe_link_id(cur, clip_mem_id, link_id):
    # Same link_id group semantics as clips.link_id (may be a clips.id or
    # another member's mem id). None clears membership.
    db_id = db_id_from_mem(clip_mem_id)
    cur.execute(
        "UPDATE keyframe_clips SET link_id = ? WHERE id = ?",
        (link_id, db_id)
    )


class AddKeyframeClipDialog(QDialog):
    # Name + type (X / Y / Scale / ... / X Crop / Y Crop) + Add
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Keyframe Clip")
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        row.addWidget(self.name_edit)
        layout.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Type:"))
        self.type_combo = QComboBox()
        for label, _stored in KEYFRAME_TYPE_CHOICES:
            self.type_combo.addItem(label)
        row2.addWidget(self.type_combo)
        layout.addLayout(row2)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.accept)
        layout.addWidget(add_btn)

    def values(self):
        name = (self.name_edit.text() or "").strip()
        idx = self.type_combo.currentIndex()
        if idx < 0 or idx >= len(KEYFRAME_TYPE_CHOICES):
            return None
        stored = KEYFRAME_TYPE_CHOICES[idx][1]
        return name, stored


def add_keyframe_clip(timeline, track_id, click_frame):
    # Dialog then insert. out_frame = in_frame + 30 * fps.
    dlg = AddKeyframeClipDialog(timeline)
    if dlg.exec() != QDialog.Accepted:
        return
    vals = dlg.values()
    if vals is None:
        return
    name, stored_type = vals
    in_frame = max(0, int(click_frame))
    try:
        fps = float(getattr(timeline, "fps", 30) or 30)
    except Exception:
        fps = 30.0
    if fps <= 0:
        fps = 30.0
    length = max(1, int(round(30.0 * fps)))
    out_frame = in_frame + length
    project_db = timeline._project_db()
    if not project_db:
        QMessageBox.warning(timeline, "Add Keyframe Clip", "No project open")
        return
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        ensure_keyframe_tables(conn)
        if keyframe_range_overlaps(cur, track_id, in_frame, length):
            conn.close()
            timeline.status_bar.showMessage(
                "Add Keyframe Clip refused: overlaps a clip on this track", 3000)
            return
        from undo import push_undo
        push_undo(project_db, "Add keyframe clip")
        cur.execute(
            "INSERT INTO keyframe_clips (sequence_id, track_id, type, link_id, "
            "in_frame, out_frame, name) VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (timeline.sequence_id, int(track_id), stored_type,
             in_frame, out_frame, name)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(
            timeline, "Add Keyframe Clip", "Could not add clip: %s" % e)
        return
    # BUILD 139: a new keyframe clip is unlinked (no effect until linked);
    # the refresh's snapshot diff marks whatever changes (nothing here)
    timeline._after_clip_change(
        "Added keyframe clip at frame %d" % in_frame)


class AddKeyDialog(QDialog):
    # One numeric value labeled with the clip type (X / Y / Scale)
    # BUILD 147: choose_type=True (the clip has no keys): a Type drop-down
    # preset to the clip's type; this key sets the clip's type. defaults:
    # callable stored_type -> initial value when the type is changed.
    def __init__(self, parent, type_label, initial=None, title="Add Key",
                 stored_type=None, choose_type=False, defaults=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        self.type_combo = None
        self._defaults = defaults
        if choose_type:
            trow = QHBoxLayout()
            trow.addWidget(QLabel("Type:"))
            self.type_combo = QComboBox()
            for label, _stored in KEYFRAME_TYPE_CHOICES:
                self.type_combo.addItem(label)
            for i, (_label, stored) in enumerate(KEYFRAME_TYPE_CHOICES):
                if stored == stored_type:
                    self.type_combo.setCurrentIndex(i)
            trow.addWidget(self.type_combo)
            layout.addLayout(trow)
        row = QHBoxLayout()
        self.value_label = QLabel("%s:" % (type_label or "Value"))
        row.addWidget(self.value_label)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(2)
        lo, hi = KEY_VALUE_RANGES.get(stored_type or "",
                                      (-1000000000.0, 1000000000.0))
        self.spin.setRange(lo, hi)
        self.stored_type = stored_type
        self.spin.setSingleStep(0.1)
        if initial is not None:
            self.spin.setValue(float(initial))
        row.addWidget(self.spin)
        layout.addLayout(row)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)
        if self.type_combo is not None:
            self.type_combo.currentIndexChanged.connect(self._type_changed)

    def _type_changed(self, idx):
        # BUILD 147: new type: its label, value range and default value
        if idx < 0 or idx >= len(KEYFRAME_TYPE_CHOICES):
            return
        label, stored = KEYFRAME_TYPE_CHOICES[idx]
        self.stored_type = stored
        self.value_label.setText("%s:" % label)
        lo, hi = KEY_VALUE_RANGES.get(stored, (-1000000000.0, 1000000000.0))
        self.spin.setRange(lo, hi)
        if self._defaults is not None:
            try:
                self.spin.setValue(float(self._defaults(stored)))
            except Exception:
                pass

    def chosen_type(self):
        # BUILD 147: the type this key is for (the clip's, or the drop-down's)
        return self.stored_type

    def value(self):
        return clamp_key_value(self.stored_type, self.spin.value())


def clip_type_from_db(cur, db_id):
    cur.execute("SELECT type FROM keyframe_clips WHERE id = ?", (int(db_id),))
    row = cur.fetchone()
    return (row[0] if row else "") or ""


def key_count_from_db(cur, db_id):
    # BUILD 147
    cur.execute("SELECT COUNT(*) FROM keyframes WHERE keyframe_clip_id = ?",
                (int(db_id),))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def set_clip_type(cur, db_id, stored_type):
    # BUILD 147: the one-type rule. A keyframe clip's type changes only while
    # it has no keys; returns True when the clip has (now) that type.
    if stored_type not in KEYFRAME_CLIP_TYPES:
        return False
    if clip_type_from_db(cur, db_id) == stored_type:
        return True
    if key_count_from_db(cur, db_id) > 0:
        return False
    cur.execute("UPDATE keyframe_clips SET type = ? WHERE id = ?",
                (stored_type, int(db_id)))
    return True


def clip_type_label_from_db(cur, db_id):
    return type_display_label(clip_type_from_db(cur, db_id)) or "Value"


def default_key_value(timeline, stored_type):
    # position X/Y = sequence center; scale = 100
    if stored_type == "position X":
        w, _h = timeline._sequence_size()
        return float(w) / 2.0
    if stored_type == "position Y":
        _w, h = timeline._sequence_size()
        return float(h) / 2.0
    if stored_type == "scale":
        return 100.0
    if stored_type == "volume":
        return 100.0
    if stored_type == "opacity":
        return 100.0
    # BUILD 154: gamma 1.0, contrast 1.0 (no change); brightness 0 below
    if stored_type in ("gamma", "contrast"):
        return 1.0
    # BUILD 147: crop_x / crop_y = 0 (no crop); others 0 as before
    return 0.0


def rename_keyframe_clip(timeline, clip):
    # Rename popup prefilled with the stored name; OK writes it (undoable)
    project_db = timeline._project_db()
    if not project_db:
        return
    db_id = db_id_from_mem(clip[0])
    try:
        conn = sqlite3.connect(project_db)
        try:
            row = conn.execute("SELECT COALESCE(name, '') FROM keyframe_clips "
                               "WHERE id = ?", (db_id,)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Rename", "Could not read name: %s" % e)
        return
    current = row[0] if row else ""
    name, ok = QInputDialog.getText(timeline, "Rename", "Name:", QLineEdit.Normal,
                                    current)
    if not ok:
        return
    name = (name or "").strip()
    if name == current:
        return
    try:
        from undo import push_undo
        push_undo(project_db, "Rename keyframe clip")
    except Exception:
        pass
    try:
        conn = sqlite3.connect(project_db)
        try:
            conn.execute("UPDATE keyframe_clips SET name = ? WHERE id = ?",
                         (name, db_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Rename", "Could not rename: %s" % e)
        return
    timeline._after_clip_change("Renamed to %s" % name)


def add_key(timeline, clip, click_frame):
    # Insert a key at the mouse frame (clamped to [in, out)).
    db_id = db_id_from_mem(clip[0])
    in_f = max(0, int(clip[2]))
    length = max(1, int(clip[3]))
    out_f = in_f + length
    frame = max(in_f, min(out_f - 1, int(click_frame)))
    project_db = timeline._project_db()
    if not project_db:
        QMessageBox.warning(timeline, "Add Key", "No project open")
        return
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        ensure_keyframe_tables(conn)
        stored_type = clip_type_from_db(cur, db_id)
        type_label = type_display_label(stored_type) or "Value"
        # BUILD 147: no keys yet = the type may be chosen with this key
        no_keys = key_count_from_db(cur, db_id) == 0
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Add Key", "Could not read clip: %s" % e)
        return
    initial = default_key_value(timeline, stored_type)
    dlg = AddKeyDialog(timeline, type_label, initial=initial,
                       stored_type=stored_type, choose_type=no_keys,
                       defaults=lambda t: default_key_value(timeline, t))
    if dlg.exec() != QDialog.Accepted:
        return
    new_type = dlg.chosen_type() if no_keys else stored_type
    value = clamp_key_value(new_type, dlg.value())
    from undo import push_undo
    push_undo(project_db, "Add key")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        ensure_keyframe_tables(conn)
        # BUILD 147: one type per clip. A different type is set only while
        # the clip has no keys (checked again in this transaction).
        if new_type != stored_type and not set_clip_type(cur, db_id, new_type):
            conn.close()
            timeline.status_bar.showMessage(
                "Add Key refused: the clip already has %s keys" % type_label,
                3000)
            return
        cur.execute(
            "INSERT INTO keyframes (keyframe_clip_id, frame, value) "
            "VALUES (?, ?, ?)",
            (db_id, frame, value)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Add Key", "Could not add key: %s" % e)
        return
    mark_keyframe_clip_preview_dirty(timeline, clip)
    timeline._after_clip_change("Added key at frame %d" % frame)


def persist_keyframe_frame(cur, key_id, frame):
    cur.execute(
        "UPDATE keyframes SET frame = ? WHERE id = ?",
        (int(frame), int(key_id))
    )


def persist_keyframe_value(cur, key_id, value):
    cur.execute(
        "UPDATE keyframes SET value = ? WHERE id = ?",
        (float(value), int(key_id))
    )


def edit_key(timeline, clip, key_id, frame, value):
    # Prefill edit popup; OK writes keyframes.value
    db_id = db_id_from_mem(clip[0])
    stored_type = clip[10] if len(clip) > 10 else ""
    type_label = type_display_label(stored_type) or "Value"
    if not stored_type:
        project_db = timeline._project_db()
        if project_db:
            try:
                conn = sqlite3.connect(project_db)
                cur = conn.cursor()
                stored_type = clip_type_from_db(cur, db_id)
                type_label = type_display_label(stored_type) or "Value"
                conn.close()
            except Exception:
                pass
    dlg = AddKeyDialog(timeline, type_label,
                       initial=clamp_key_value(stored_type, value),
                       title="Edit Key", stored_type=stored_type)
    if dlg.exec() != QDialog.Accepted:
        return
    new_val = clamp_key_value(stored_type, dlg.value())
    project_db = timeline._project_db()
    if not project_db:
        return
    from undo import push_undo
    push_undo(project_db, "Edit key")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        persist_keyframe_value(cur, key_id, new_val)
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Edit Key", "Could not save: %s" % e)
        return
    mark_keyframe_clip_preview_dirty(timeline, clip)
    timeline._after_clip_change("Updated key at frame %d" % int(frame))


def hit_test_key(timeline, pos):
    # Topmost key square under pos on a keyframe track, or None
    from sequence_timeline_ruler import CLIPS_COL
    row = timeline.table.rowAt(pos.y())
    if row < 0:
        return None
    name_item = timeline.table.item(row, 0)
    if name_item is None:
        return None
    if (name_item.data(Qt.UserRole + 4) or "").lower() != "keyframe":
        return None
    track_id = name_item.data(Qt.UserRole)
    x_in_clips = timeline._x_in_clips(pos)
    if x_in_clips < 0:
        return None
    cell = timeline.table.visualRect(timeline.table.model().index(row, CLIPS_COL))
    if cell.isNull():
        return None
    ppf = timeline.pixels_per_frame
    fm = timeline.table.fontMetrics()
    # Later clips on top (same as _clip_at reversed)
    for clip in reversed(timeline.clips_by_track.get(track_id, [])):
        if not is_keyframe_clip_id(clip[0]):
            continue
        start = int(clip[2])
        length = max(1, int(clip[3]))
        x = int(cell.left() + (start - timeline.view_start_frame) * ppf)
        w = max(1, int(length * ppf))
        clip_rect = QRect(x, cell.top(), w, cell.height())
        keys = clip[9] if len(clip) > 9 else []
        stored_type = clip[10] if len(clip) > 10 else ""
        for i in range(len(keys) - 1, -1, -1):
            item = keys[i]
            if len(item) >= 3:
                key_id, frame, value = item[0], item[1], item[2]
            else:
                continue
            if not (start <= int(frame) < start + length):
                # hidden key outside the clip (after an edge trim): not
                # drawn, so not hittable
                continue
            _text, sq, _cx, _bt = key_mark_layout(
                clip_rect, frame, value, start, ppf, stored_type, fm)
            if sq.contains(pos):
                return {
                    "clip": clip,
                    "index": i,
                    "key_id": int(key_id),
                    "frame": int(frame),
                    "value": float(value),
                }
    return None


def key_drag_move(timeline, pos):
    drag = timeline._key_drag
    if drag is None:
        return
    press = drag["press_pos"]
    dist = (pos - press)
    threshold = max(1, QApplication.startDragDistance())
    if not drag["dragging"]:
        if abs(dist.x()) < threshold and abs(dist.y()) < threshold:
            return
        drag["dragging"] = True
    clip = drag["clip"]
    in_f = max(0, int(clip[2]))
    out_f = in_f + max(1, int(clip[3]))
    frame = timeline._frame_at_clips_x(timeline._x_in_clips(pos))
    frame = max(in_f, min(out_f - 1, int(frame)))
    idx = drag["index"]
    if 0 <= idx < len(clip[9]):
        kid, _old_f, val = clip[9][idx]
        clip[9][idx] = (kid, frame, val)
    drag["frame"] = frame
    timeline.table.viewport().update()




def key_drag_release(timeline):
    # Single click/drag: move only. Edit is double-click / context menu.
    drag = timeline._key_drag
    timeline._key_drag = None
    if drag is None:
        return
    clip = drag["clip"]
    if not drag.get("dragging"):
        return
    frame = int(drag.get("frame", drag["orig_frame"]))
    in_f = max(0, int(clip[2]))
    out_f = in_f + max(1, int(clip[3]))
    frame = max(in_f, min(out_f - 1, frame))
    project_db = timeline._project_db()
    if not project_db:
        timeline.table.viewport().update()
        return
    if frame != int(drag["orig_frame"]):
        from undo import push_undo
        push_undo(project_db, "Move key")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        persist_keyframe_frame(cur, drag["key_id"], frame)
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Move Key", "Could not save: %s" % e)
        timeline._after_clip_change("Key move failed")
        return
    mark_keyframe_clip_preview_dirty(timeline, clip)
    timeline._after_clip_change("Moved key to frame %d" % frame)


def delete_key(timeline, key_id, clip=None):
    project_db = timeline._project_db()
    if not project_db:
        return
    from undo import push_undo
    push_undo(project_db, "Delete key")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("DELETE FROM keyframes WHERE id = ?", (int(key_id),))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Delete Keyframe", "Could not delete: %s" % e)
        return
    mark_keyframe_clip_preview_dirty(timeline, clip)
    timeline._after_clip_change("Deleted keyframe")


def key_context_menu(timeline, key_hit, pos):
    # Right-click on a key box: Edit Keyframe / Delete Keyframe
    from PySide6.QtWidgets import QMenu
    menu = QMenu(timeline)
    edit_action = menu.addAction("Edit Keyframe")
    delete_action = menu.addAction("Delete Keyframe")
    action = menu.exec(timeline.table.viewport().mapToGlobal(pos))
    if action == edit_action:
        edit_key(timeline, key_hit["clip"], key_hit["key_id"],
                 key_hit["frame"], key_hit["value"])
    elif action == delete_action:
        delete_key(timeline, key_hit["key_id"], key_hit.get("clip"))

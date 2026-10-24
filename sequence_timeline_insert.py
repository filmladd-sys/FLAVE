# sequence_timeline_insert.py
# Shift+drag insert for the Sequence Timeline. While Shift is held during a
# clip (or linked stack) move, the dragged clips may pass over other clips.
# Dropping with Shift held inserts them:
#   - the insert frame is the earliest start among the dragged clips at the
#     drop, the insert span runs from there to their latest end;
#   - every other clip on every track of the sequence that spans the insert
#     frame is sliced there (same cut as Slice, links kept per side);
#   - every other clip that starts at or after the insert frame (the right
#     pieces too) moves right by the span, on every track, so all tracks
#     stay in sync (keyframe clips carry their keys);
#   - the dragged clips land at their drop position, in the gap.
# Where the dragged clips came from is left as a gap. One undo step.
# Functions take the SequenceTimeline as tl. ASCII only.

import sqlite3
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox


def insert_held():
    # True while Shift is down (live keyboard state, not the event's)
    return bool(QApplication.queryKeyboardModifiers() & Qt.ShiftModifier)


def _not_in(column, ids):
    # SQL "AND column NOT IN (...)" plus its parameters ("" when no ids)
    ids = list(ids)
    if not ids:
        return "", []
    return f" AND {column} NOT IN ({','.join('?' * len(ids))})", ids


def insert_cuts(tl, at, ids=()):
    # Clips (any track or type, except ids) that span frame at (start < at
    # < end), one entry (members, linked) per link group so the right
    # pieces of a linked stack share one new link_id. Read from memory
    # before the undo step.
    cuts = []
    seen = set()
    for clips in tl.clips_by_track.values():
        for c in clips:
            if c[0] in ids or c[0] in seen or not (c[2] < at < c[2] + c[3]):
                continue
            members = [c] + [p for p in tl._linked_clips(c) if p[0] not in ids]
            seen.update(p[0] for p in members)
            cuts.append((members, len(c) > 8 and c[8] is not None))
    return cuts


def ripple_insert_db(tl, cur, at, span, cuts, rows=None):
    # The database part of an insert, in the caller's transaction and undo
    # step: slice every cut at frame at (slice_members_db, the Slice cut),
    # then move every clip, caption and keyframe clip of the sequence that
    # starts at or after at (the right pieces too) right by span, on every
    # track; keyframe clips carry their keys, markers are source-relative
    # and follow their clips. rows: {table: [row ids]} left in place (the
    # dragged clips of a Shift+drag insert); None = none.
    from keyframe import shift_clip_keys
    from sequence_timeline_sql import slice_members_db
    rows = rows or {}
    for members, linked in cuts:
        slice_members_db(tl, cur, members, at, linked)
    seq = tl.sequence_id
    sql, extra = _not_in("id", rows.get("clips", ()))
    cur.execute(
        "UPDATE clips SET start_frame = start_frame + ? WHERE start_frame >= ? "
        "AND track_id IN (SELECT id FROM tracks WHERE sequence_id = ?)" + sql,
        [span, at, seq] + extra)
    sql, extra = _not_in("id", rows.get("captions", ()))
    cur.execute(
        "UPDATE captions SET start_frame = start_frame + ? "
        "WHERE start_frame >= ? AND sequence_id = ?" + sql,
        [span, at, seq] + extra)
    # WYSIWYG Text clips (text_clips)
    sql, extra = _not_in("id", rows.get("text_clips", ()))
    cur.execute(
        "UPDATE text_clips SET start_frame = start_frame + ? "
        "WHERE start_frame >= ? AND sequence_id = ?" + sql,
        [span, at, seq] + extra)
    sql, extra = _not_in("id", rows.get("keyframe_clips", ()))
    cur.execute(
        "SELECT id FROM keyframe_clips WHERE in_frame >= ? AND sequence_id = ?" + sql,
        [at, seq] + extra)
    kf_ids = [r[0] for r in cur.fetchall()]
    for kid in kf_ids:
        cur.execute("UPDATE keyframe_clips SET in_frame = in_frame + ?, "
                    "out_frame = out_frame + ? WHERE id = ?", (span, span, kid))
        # keyframes / keyframe_border rows
        shift_clip_keys(cur, kid, span)


def insert_drop(tl, group):
    # group: the drag group (dicts with "clip"); the clips already hold
    # their drop positions in memory
    from keyframe import is_keyframe_clip_id, persist_keyframe_clip_time
    from sequence_timeline_captions import clip_table_and_id
    moving = [m["clip"] for m in group]
    if not moving or tl.sequence_id is None:
        return
    ids = {c[0] for c in moving}
    at = max(0, min(int(c[2]) for c in moving))
    span = max(int(c[2]) + int(c[3]) for c in moving) - at
    cuts = insert_cuts(tl, at, ids)
    rows = {"clips": [], "captions": [], "keyframe_clips": [], "text_clips": []}
    for c in moving:
        table, row_id = clip_table_and_id(c[0])
        rows[table].append(row_id)
    from undo import push_undo
    push_undo(tl._project_db(), "Insert clips")
    try:
        conn = sqlite3.connect(tl._project_db())
        cur = conn.cursor()
        ripple_insert_db(tl, cur, at, span, cuts, rows)
        # The dragged clips at their drop positions (keyframe clips carry
        # their keys, as in a normal move)
        for c in moving:
            if is_keyframe_clip_id(c[0]):
                persist_keyframe_clip_time(cur, c)
                continue
            table, row_id = clip_table_and_id(c[0])
            cur.execute(f"UPDATE {table} SET start_frame = ? WHERE id = ?",
                        (max(0, int(c[2])), row_id))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(tl, "Insert Clips", f"Could not insert clips: {e}")
        tl._after_clip_change("Insert failed")
        return
    tl._after_clip_change(
        f"Inserted at frame {at}: {len(cuts)} cut(s), later clips pushed {span} frames")


# ---------------------------------------------------------------- cut line
# While a move drag runs with Shift held, a white line through the ruler
# and every track marks tl._insert_frame, the frame the drop will cut at
# (painted in sequence_timeline_ruler.py next to the snap line). Pressing
# or releasing Shift mid-drag is picked up by a short timer that re-runs
# the drag at the last mouse position, so the line and the clip position
# switch without moving the mouse.

INSERT_WATCH_MS = 50


def set_insert_frame(tl, frame):
    # Cut line frame (None = hidden); repaint only when it changes
    if getattr(tl, "_insert_frame", None) == frame:
        return
    tl._insert_frame = frame
    try:
        tl.ruler.viewport().update()
        tl.playhead_overlay.update()
    except Exception:
        pass


def insert_watch(tl, pos, active):
    # Called by each move-drag step: remember the mouse and Shift state,
    # keep the Shift watch timer running while the drag lasts
    drag = tl._edge_drag
    drag["last_pos"] = pos
    drag["insert_state"] = active
    timer = getattr(tl, "_insert_timer", None)
    if timer is None:
        from PySide6.QtCore import QTimer
        timer = QTimer(tl)
        timer.setInterval(INSERT_WATCH_MS)
        timer.timeout.connect(lambda: _insert_tick(tl))
        tl._insert_timer = timer
    if not timer.isActive():
        timer.start()


def _insert_tick(tl):
    drag = getattr(tl, "_edge_drag", None)
    if drag is None or drag.get("edge") != "move":
        tl._insert_timer.stop()
        set_insert_frame(tl, None)
        return
    if insert_held() != drag.get("insert_state") and drag.get("last_pos") is not None:
        tl._edge_drag_move(drag["last_pos"])

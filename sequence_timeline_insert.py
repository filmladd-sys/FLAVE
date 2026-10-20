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


def insert_drop(tl, group):
    # group: the drag group (dicts with "clip"); the clips already hold
    # their drop positions in memory
    from keyframe import is_keyframe_clip_id, persist_keyframe_clip_time
    from sequence_timeline_captions import clip_table_and_id
    from sequence_timeline_sql import slice_members_db
    moving = [m["clip"] for m in group]
    if not moving or tl.sequence_id is None:
        return
    ids = {c[0] for c in moving}
    at = max(0, min(int(c[2]) for c in moving))
    span = max(int(c[2]) + int(c[3]) for c in moving) - at
    # Clips that span the insert frame, one entry per link group so the
    # right pieces of a linked stack share one new link_id
    cuts = []
    seen = set()
    for clips in tl.clips_by_track.values():
        for c in clips:
            if c[0] in ids or c[0] in seen or not (c[2] < at < c[2] + c[3]):
                continue
            members = [c] + [p for p in tl._linked_clips(c) if p[0] not in ids]
            seen.update(p[0] for p in members)
            cuts.append((members, len(c) > 8 and c[8] is not None))
    rows = {"clips": [], "captions": [], "keyframe_clips": []}
    for c in moving:
        table, row_id = clip_table_and_id(c[0])
        rows[table].append(row_id)
    from undo import push_undo
    push_undo(tl._project_db(), "Insert clips")
    try:
        conn = sqlite3.connect(tl._project_db())
        cur = conn.cursor()
        for members, linked in cuts:
            slice_members_db(tl, cur, members, at, linked)
        seq = tl.sequence_id
        sql, extra = _not_in("id", rows["clips"])
        cur.execute(
            "UPDATE clips SET start_frame = start_frame + ? WHERE start_frame >= ? "
            "AND track_id IN (SELECT id FROM tracks WHERE sequence_id = ?)" + sql,
            [span, at, seq] + extra)
        sql, extra = _not_in("id", rows["captions"])
        cur.execute(
            "UPDATE captions SET start_frame = start_frame + ? "
            "WHERE start_frame >= ? AND sequence_id = ?" + sql,
            [span, at, seq] + extra)
        sql, extra = _not_in("id", rows["keyframe_clips"])
        cur.execute(
            "SELECT id FROM keyframe_clips WHERE in_frame >= ? AND sequence_id = ?" + sql,
            [at, seq] + extra)
        kf_ids = [r[0] for r in cur.fetchall()]
        for kid in kf_ids:
            cur.execute("UPDATE keyframe_clips SET in_frame = in_frame + ?, "
                        "out_frame = out_frame + ? WHERE id = ?", (span, span, kid))
            cur.execute("UPDATE keyframes SET frame = frame + ? "
                        "WHERE keyframe_clip_id = ?", (span, kid))
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

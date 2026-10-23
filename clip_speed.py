# clip_speed.py
# Clip speed (BUILD 303): one integer percent per timeline clip, 1 .. 1000,
# default 100, stored in speed_percent on clips, captions and keyframe_clips
# (new_project.py; ensure_speed_columns adds it to an older project on
# open). The timeline keeps {timeline clip id: speed} for clips whose speed
# is not 100 (tl._clip_speeds, load_clip_speeds); the player snapshot copies
# it (snapshot[28]).
#
# Mapping: a clip keeps its in_frame (a sequence-rate source frame, as
# before) and timeline frame start + t shows source position
# in + t * speed / 100 (source_pos). Video floors that position once,
# after fps conform and any proxy rate (sequence_player.clip_source_frame);
# audio reads it sample exact with linear interpolation (tape style, pitch
# follows speed; sequence_preview_audio). At 100 every value is exactly the
# integer it was before speed existed.
#
# Speed... (edit_clip_speed): the right-clicked clip and every clip linked
# to it get the new speed and one new timeline length,
# round(length * old / new), cut to the room before the next clip on each
# member's track and to the footage end (BUILD 301 source cap) of each
# footage member; never below 1 frame; later clips do not move. Keys of a
# keyframe clip scale about its start. One undo step. ASCII only.

import math
import sqlite3

SPEED_MIN = 1
SPEED_MAX = 1000
SPEED_DEFAULT = 100
SPEED_COLUMN = "speed_percent"
# Tables whose rows are timeline clips
SPEED_TABLES = ("clips", "captions", "keyframe_clips")


def ensure_speed_columns(conn):
    # TEMPORARY older-project check (BUILD 303): add speed_percent to each
    # clip table that lacks it (new_project.py creates it). Remove in the
    # future, once every project in use has the column.
    for table in SPEED_TABLES:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
        if cols and SPEED_COLUMN not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER NOT NULL "
                         "DEFAULT %d" % (table, SPEED_COLUMN, SPEED_DEFAULT))


def has_speed_column(cur, table):
    cur.execute("PRAGMA table_info(%s)" % table)
    return SPEED_COLUMN in [r[1] for r in cur.fetchall()]


def clamp_speed(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return SPEED_DEFAULT
    return max(SPEED_MIN, min(SPEED_MAX, v))


def load_clip_speeds(cur, sequence_id):
    # {timeline clip id: speed} of this sequence's clips, captions and
    # keyframe clips whose speed is not 100 (ids as in clips_by_track:
    # clips.id, -captions.id, keyframe.mem_id_from_db)
    from keyframe import mem_id_from_db
    out = {}
    queries = (
        ("clips", "SELECT c.id, c.%s FROM clips c JOIN tracks t ON t.id = "
                  "c.track_id WHERE t.sequence_id = ? AND c.%s != %d",
         lambda i: i),
        ("captions", "SELECT id, %s FROM captions WHERE sequence_id = ? "
                     "AND %s != %d", lambda i: -i),
        ("keyframe_clips", "SELECT id, %s FROM keyframe_clips WHERE "
                           "sequence_id = ? AND %s != %d", mem_id_from_db),
    )
    for table, sql, to_mem in queries:
        try:
            if not has_speed_column(cur, table):
                continue
            cur.execute(sql % (SPEED_COLUMN, SPEED_COLUMN, SPEED_DEFAULT),
                        (sequence_id,))
            for row_id, speed in cur.fetchall():
                if speed is not None and int(speed) != SPEED_DEFAULT:
                    out[to_mem(int(row_id))] = clamp_speed(speed)
        except sqlite3.Error:
            continue
    return out


def speed_of(speeds, clip_id):
    # Speed of a timeline clip id from a load_clip_speeds map
    if not speeds:
        return SPEED_DEFAULT
    return speeds.get(clip_id, SPEED_DEFAULT)


def source_pos(in_frame, rel, speed):
    # Source position (sequence-rate frames) at rel frames after the clip
    # start: in + rel exactly at 100, else the float in + rel * speed / 100
    if speed == SPEED_DEFAULT:
        return in_frame + rel
    return in_frame + rel * speed / 100.0


def source_offset(rel, speed):
    # Whole source frames from the in-point at rel timeline frames:
    # floor(rel * speed / 100) (also for rel < 0), exact integer math
    if speed == SPEED_DEFAULT:
        return int(rel)
    return (int(rel) * int(speed)) // 100


def source_span(length, speed):
    # Source frames a clip of length timeline frames covers: its last frame
    # shows in + source_offset(length - 1), so in .. in + span - 1
    length = max(1, int(length))
    if speed == SPEED_DEFAULT:
        return length
    return source_offset(length - 1, speed) + 1


def timeline_offset(src_rel, speed):
    # First timeline offset t >= 0 whose source_offset(t) >= src_rel
    if speed == SPEED_DEFAULT:
        return int(src_rel)
    return -((-int(src_rel) * 100) // int(speed))


def max_length_for_source(avail, speed):
    # Longest timeline length whose source_span fits in avail source frames
    # (at least 1)
    avail = int(avail)
    if avail <= 0:
        return 1
    if speed == SPEED_DEFAULT:
        return avail
    return max(1, -((-avail * 100) // int(speed)))


def speed_length(length, old_speed, new_speed):
    # Timeline length at new_speed for the same source range:
    # round(length * old / new), halves up, at least 1
    num = int(length) * int(old_speed)
    den = int(new_speed)
    return max(1, (2 * num + den) // (2 * den))


def fit_speed_length(length, old_speed, new_speed, rooms=(), source_rooms=()):
    # New timeline length: speed_length, cut to each member's room before
    # its next clip (rooms, None = open) and to each footage member's
    # remaining source frames (source_rooms: cap - in, in sequence-rate
    # frames) at new_speed; tightest wins, never below 1
    new_len = speed_length(length, old_speed, new_speed)
    for room in rooms:
        if room is not None:
            new_len = min(new_len, int(room))
    for avail in source_rooms:
        if avail is not None:
            new_len = min(new_len, max_length_for_source(avail, new_speed))
    return max(1, new_len)


def scale_key_frame(frame, start, old_speed, new_speed):
    # A key at frame keeps its place in the clip's content: its offset from
    # the clip start scales by old / new (round, halves up)
    off = int(frame) - int(start)
    num = off * int(old_speed)
    den = int(new_speed)
    return int(start) + (2 * num + den) // (2 * den)


def speed_label(speed):
    # Timeline clip label suffix when the speed is not 100, e.g. " 200%"
    if speed is None or int(speed) == SPEED_DEFAULT:
        return ""
    return " %d%%" % int(speed)


def label_for(tl, clip_id):
    return speed_label(speed_of(getattr(tl, "_clip_speeds", None), clip_id))


def row_speed(cur, table, row_id):
    # Stored speed of one row (100 when the column or row is missing)
    try:
        row = cur.execute("SELECT %s FROM %s WHERE id = ?" % (SPEED_COLUMN, table),
                          (int(row_id),)).fetchone()
    except sqlite3.Error:
        return SPEED_DEFAULT
    return clamp_speed(row[0]) if row and row[0] is not None else SPEED_DEFAULT


def copy_speed(cur, table, src_id, dst_id):
    # A new row made from another (slice) keeps its speed
    try:
        cur.execute("UPDATE %s SET %s = (SELECT %s FROM %s WHERE id = ?) "
                    "WHERE id = ?" % (table, SPEED_COLUMN, SPEED_COLUMN, table),
                    (int(src_id), int(dst_id)))
    except sqlite3.Error:
        pass


# Audio (tape style, no pitch preservation): output sample i of a clip
# (offset from its first timeline sample) reads source sample position
# src0 + i * speed / 100, linear between the two nearest source samples.
# All index math is integer so a mix chunk and a partial re-mix of any
# range give the same samples as one full pass.

def speed_take(count, slen, src0, speed):
    # Output samples a clip can fill: count, cut where its source position
    # reaches the end of the slen source samples
    left = int(slen) - int(src0)
    if left <= 0:
        return 0
    if speed == SPEED_DEFAULT:
        return min(int(count), left)
    return min(int(count), -((-left * 100) // int(speed)))


def speed_source_range(src0, a, b, speed, slen):
    # Source samples [lo, hi) that output offsets a .. b - 1 read
    # (the sample after the last position included for interpolation)
    lo = int(src0) + (int(a) * int(speed)) // 100
    hi = int(src0) + ((int(b) - 1) * int(speed)) // 100 + 2
    return lo, min(int(slen), hi)


def speed_resample(np, src, src0, a, b, speed, base=0, slen=None):
    # float64 samples for output offsets a .. b - 1 at speed. src: int16
    # source samples [base, base + len(src)) of a source slen samples long
    # (default: the whole source); every read position must lie inside it
    # (speed_take, speed_source_range)
    if slen is None:
        slen = int(base) + len(src)
    i = np.arange(int(a), int(b), dtype=np.int64) * int(speed)
    idx = int(src0) + i // 100
    frac = (i % 100).astype(np.float64) / 100.0
    nxt = np.minimum(idx + 1, int(slen) - 1) - int(base)
    lo = src[idx - int(base)].astype(np.float64)
    return lo + (src[nxt].astype(np.float64) - lo) * frac


def speed_sample(src, src0, i, speed):
    # One output sample (no numpy): the same arithmetic as speed_resample
    p = int(i) * int(speed)
    idx = int(src0) + p // 100
    frac = (p % 100) / 100.0
    lo = float(src[idx])
    hi = float(src[min(idx + 1, len(src) - 1)])
    return lo + (hi - lo) * frac


def apply_speed_db(cur, members, new_speed, new_len):
    # Write new_speed and length new_len to every member (live clip lists
    # of the timeline). Keyframe clips: out_frame, and every key's frame
    # scaled about the start (scale_key_frame) from its old speed. No
    # commit / undo (the caller owns them).
    from sequence_timeline_captions import clip_table_and_id
    from keyframe import key_tables
    for c in members:
        table, row_id = clip_table_and_id(c[0])
        old = row_speed(cur, table, row_id)
        start = int(c[2])
        if table == "keyframe_clips":
            cur.execute("UPDATE keyframe_clips SET out_frame = ?, %s = ? "
                        "WHERE id = ?" % SPEED_COLUMN,
                        (start + new_len, int(new_speed), row_id))
            if old != new_speed:
                for kt in key_tables(cur):
                    rows = cur.execute(
                        "SELECT rowid, frame FROM %s WHERE keyframe_clip_id = ?"
                        % kt, (row_id,)).fetchall()
                    for rid, frame in rows:
                        cur.execute("UPDATE %s SET frame = ? WHERE rowid = ?" % kt,
                                    (scale_key_frame(frame, start, old, new_speed),
                                     rid))
        else:
            cur.execute("UPDATE %s SET length_frames = ?, %s = ? WHERE id = ?"
                        % (table, SPEED_COLUMN), (new_len, int(new_speed), row_id))


def _members_and_rooms(tl, clip):
    # (members, rooms, footage members) for edit_clip_speed: the clip and
    # its link partners, each member's room before its next clip (others of
    # the stack ignored), and the members with a footage source cap
    members = [clip] + tl._linked_clips(clip)
    ids = {c[0] for c in members}
    rooms = []
    for c in members:
        _prev, nxt = tl._neighbor_bounds(c, c[2], c[3], ignore_ids=ids)
        rooms.append(None if nxt is None else max(1, nxt - int(c[2])))
    return members, rooms


def _source_rooms(tl, members):
    # Remaining source frames (cap - in) of each footage member with a
    # BUILD 301 source cap (video / audio footage clips)
    from sequence_timeline_clips import _add_source_caps, _no_source_floor
    group = [{"clip": c, "free_in": _no_source_floor(tl, c)} for c in members]
    _add_source_caps(tl, group)
    return [m["src_end"] - int(m["clip"][7]) for m in group if m.get("src_end")]


def edit_clip_speed(tl, clip):
    # Speed... on any clip: dialog, then the whole linked stack in one
    # undo step (see the module header)
    from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                   QSpinBox, QDialogButtonBox, QMessageBox)
    project_db = tl._project_db()
    if not project_db or clip is None:
        return
    old = speed_of(getattr(tl, "_clip_speeds", None), clip[0])
    dlg = QDialog(tl)
    dlg.setWindowTitle("Speed")
    layout = QVBoxLayout(dlg)
    row = QHBoxLayout()
    row.addWidget(QLabel("Speed (%):"))
    spin = QSpinBox()
    spin.setRange(SPEED_MIN, SPEED_MAX)
    spin.setValue(old)
    spin.setSuffix(" %")
    row.addWidget(spin)
    layout.addLayout(row)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    if dlg.exec() != QDialog.Accepted:
        return
    new = clamp_speed(spin.value())
    members, rooms = _members_and_rooms(tl, clip)
    if new == old and all(speed_of(tl._clip_speeds, c[0]) == new for c in members):
        return
    try:
        src_rooms = _source_rooms(tl, members)
    except Exception:
        src_rooms = []
    new_len = fit_speed_length(clip[3], old, new, rooms, src_rooms)
    from undo import push_undo
    push_undo(project_db, "Clip speed")
    try:
        conn = sqlite3.connect(project_db)
        try:
            apply_speed_db(conn.cursor(), members, new, new_len)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        QMessageBox.warning(tl, "Speed", "Could not set speed: %s" % e)
        return
    tl._after_clip_change("Speed %d%% (%d frames)" % (new, new_len))

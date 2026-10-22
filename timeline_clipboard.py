# timeline_clipboard.py
# In-memory Copy / Cut / Paste for the Sequence Timeline. One clipboard per
# project file: the last Copy or Cut in any of its sequences (never the
# system clipboard). Stores row data, not ids, plus the source sequence id,
# its fps and the rows of the source tracks that hold copied items.
# Paste (Ctrl+V, Paste To Tic, Paste to End, right-click Paste) pastes into
# the sequence the clipboard came from, or into another sequence onto the
# selected tracks when they match the source tracks (cross_track_map);
# Paste w/New Tracks pastes into any sequence of the project on new tracks
# (paste_new_tracks). ASCII only.

import math
import os
import sqlite3

# The last Copy / Cut: dict(project=abs project path, sequence_id, fps,
# tracks=[source track rows as dicts, top to bottom], items=[item dicts]),
# or None
_CLIPBOARD = None


def _project_key(tl):
    return os.path.abspath(tl._project_db() or "")


def _clipboard(tl):
    # The clipboard when it belongs to this timeline's project, else None
    if tl.sequence_id is None or _CLIPBOARD is None:
        return None
    if _CLIPBOARD["project"] != _project_key(tl) or not _CLIPBOARD["items"]:
        return None
    return _CLIPBOARD


def has_clipboard(tl):
    # True when plain Paste can run: the clipboard came from this sequence,
    # or from another one and the selected tracks match (cross_track_map)
    clip = _clipboard(tl)
    if clip is None:
        return False
    if int(clip["sequence_id"]) == int(tl.sequence_id):
        return True
    return cross_track_map(tl, clip)[0] is not None


def _source_tracks(clip):
    # Source track rows that hold copied items, top to bottom
    return [t for t in clip["tracks"]
            if any(it["track_id"] == t.get("id") for it in clip["items"])]


def _type_label(track_type):
    from track_names import TRACK_NAME_PREFIX
    t = (track_type or "").lower()
    return TRACK_NAME_PREFIX.get(t, t or "Track")


def cross_track_map(tl, clip):
    # BUILD 290: plain Paste into another sequence. The selected tracks of
    # this sequence (tl._selected_track_ids, in track order) must match the
    # source tracks holding copied items: same count, same type at each
    # position. Returns ({source track id: selected track id}, None) when
    # they match, else (None, hint) with a status bar hint naming what to
    # select.
    src = _source_tracks(clip)
    selected = set(getattr(tl, "_selected_track_ids", ()) or ())
    dst = []
    if selected:
        try:
            conn = sqlite3.connect(tl._project_db())
            try:
                dst = [r for r in conn.execute(
                    "SELECT id, track_type FROM tracks WHERE sequence_id = ? "
                    "ORDER BY sort_order, id", (int(tl.sequence_id),))
                    if r[0] in selected]
            finally:
                conn.close()
        except sqlite3.Error:
            dst = []
    if src and len(dst) == len(src) and all(
            (d[1] or "").lower() == (t.get("track_type") or "").lower()
            for d, t in zip(dst, src)):
        return {t["id"]: d[0] for t, d in zip(src, dst)}, None
    names = ", ".join(_type_label(t.get("track_type")) for t in src)
    return None, (f"Paste: the clipboard is from another sequence; select "
                  f"{len(src)} track(s) top to bottom: {names}, or use "
                  f"Edit > Paste w/New Tracks")


def has_any_clipboard(tl):
    # True when the clipboard came from any sequence of this project
    # (Paste w/New Tracks)
    return _clipboard(tl) is not None


def _sequence_fps(cur, sequence_id):
    try:
        r = cur.execute("SELECT fps FROM sequences WHERE id = ?",
                        (int(sequence_id),)).fetchone()
        return float(r[0]) if r and r[0] else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def _selection_set(tl):
    # Same set as the Delete path: the selection plus link partners, unless
    # selection solo (then only the selected clips)
    ids = set(tl._selected_clip_ids)
    if not tl._selection_solo:
        for c in tl._selected_clips():
            for m in tl._linked_clips(c):
                ids.add(m[0])
    return ids


def _columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _start_len(table, row):
    if table == "keyframe_clips":
        s = int(row["in_frame"])
        return s, max(1, int(row["out_frame"]) - s)
    return int(row["start_frame"]), max(1, int(row["length_frames"]))


def copy_selection(tl):
    # Snapshot the selected set into the project clipboard, with this
    # sequence's id and fps and the rows of the tracks holding the items.
    # Returns the item count (0 = nothing copied, clipboard unchanged).
    global _CLIPBOARD
    if tl.sequence_id is None or not tl._selected_clip_ids:
        return 0
    from sequence_timeline_captions import clip_table_and_id
    ids = _selection_set(tl)
    items = []
    conn = sqlite3.connect(tl._project_db())
    try:
        cur = conn.cursor()
        cols_by_table = {}
        for cid in ids:
            table, row_id = clip_table_and_id(cid)
            if table not in cols_by_table:
                cols_by_table[table] = _columns(cur, table)
            cols = cols_by_table[table]
            cur.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
            r = cur.fetchone()
            if r is None:
                continue
            row = dict(zip(cols, r))
            start, length = _start_len(table, row)
            keys = []
            if table == "keyframe_clips":
                # BUILD 222: from keyframes or (border clip) keyframe_border;
                # each key dict remembers its table as "_table"
                from keyframe import key_tables
                for ktable in key_tables(cur):
                    kcols = _columns(cur, ktable)
                    cur.execute(f"SELECT * FROM {ktable} WHERE keyframe_clip_id = ?",
                                (row_id,))
                    for kr in cur.fetchall():
                        kd = dict(zip(kcols, kr))
                        kd.pop("id", None)
                        kd.pop("keyframe_clip_id", None)
                        kd["_table"] = ktable
                        keys.append(kd)
            link_id = row.get("link_id")
            row.pop("id", None)
            from markers import marker_rows
            # Markers only on clips-table rows (video / audio / image)
            marks = marker_rows(cur, cid) if table == "clips" else []
            items.append({"table": table, "row": row, "track_id": row.get("track_id"),
                          "start": start, "length": length, "link_id": link_id,
                          "keys": keys, "markers": marks})
        fps = _sequence_fps(cur, tl.sequence_id)
        used = {it["track_id"] for it in items}
        tcols = _columns(cur, "tracks")
        cur.execute("SELECT * FROM tracks WHERE sequence_id = ? "
                    "ORDER BY sort_order, id", (int(tl.sequence_id),))
        tracks = [d for d in (dict(zip(tcols, r)) for r in cur.fetchall())
                  if d.get("id") in used]
    finally:
        conn.close()
    if not items:
        return 0
    first = min(it["start"] for it in items)
    for it in items:
        it["offset"] = it["start"] - first
    _CLIPBOARD = {"project": _project_key(tl), "sequence_id": int(tl.sequence_id),
                  "fps": fps, "tracks": tracks, "items": items}
    return len(items)


def cut_selection(tl):
    # Copy, then the existing Delete path (it takes the undo snapshot)
    if tl._edge_drag is not None:
        return
    n = copy_selection(tl)
    if not n:
        return
    ranges = [(it["start"], it["start"] + it["length"] - 1)
              for it in _CLIPBOARD["items"]]
    tl._mark_preview_dirty(ranges)
    tl.delete_selected_clip()


def end_anchor(tl):
    # Latest out-point of any clip in the sequence (0 if none). A clipboard
    # from another sequence pasted onto matching selected tracks: latest
    # out-point on those tracks only.
    only = None
    clip = _clipboard(tl)
    if clip is not None and int(clip["sequence_id"]) != int(tl.sequence_id):
        track_map = cross_track_map(tl, clip)[0]
        if track_map:
            only = set(track_map.values())
    end = 0
    for tid, clips in tl.clips_by_track.items():
        if only is not None and tid not in only:
            continue
        for c in clips:
            end = max(end, int(c[2]) + int(c[3]))
    return end


def paste(tl, anchor, make_room):
    # Paste the clipboard with its earliest in-point at anchor when it came
    # from this sequence; from another sequence: nothing pasted, a status
    # bar hint names Edit > Paste w/New Tracks.
    # Overwrite: existing clips under a pasted item are trimmed, moved in or
    # deleted (see the plan below). make_room is kept for callers; Paste To
    # Tic and Paste to End both overwrite.
    # BUILD 160: every paste path (Ctrl+V, Edit menu Paste To Tic / Paste to
    # End, right-click Paste > To Mouse / To Tick) overwrites the same way:
    # covered clips removed, partly covered trimmed, a clip the paste lands
    # inside split around it (overwrite_plan / apply_overwrite).
    clip = _clipboard(tl)
    if clip is None or tl._edge_drag is not None:
        return
    if int(clip["sequence_id"]) != int(tl.sequence_id):
        _paste_cross(tl, clip, anchor)
        return
    items = clip["items"]
    anchor = max(0, int(anchor))
    project = tl._project_db()
    conn = sqlite3.connect(project)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM tracks WHERE sequence_id = ?", (int(tl.sequence_id),))
        tracks = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    ok = [it for it in items if it["track_id"] in tracks]
    skipped = len(items) - len(ok)
    if not ok:
        tl.status_bar.showMessage(
            f"Paste: {skipped} item(s) skipped, their tracks no longer exist", 5000)
        return
    _paste_core(tl, ok, anchor, skipped)


def _paste_cross(tl, clip, anchor):
    # BUILD 290: the clipboard from another sequence onto the selected
    # tracks (cross_track_map), source track i -> selected track i. Items
    # are converted to this sequence's rate (convert_items) and pasted by
    # the same core as a same-sequence paste, overwriting what is under
    # them on those tracks. No match: a status bar hint, nothing pasted.
    track_map, hint = cross_track_map(tl, clip)
    if track_map is None:
        tl.status_bar.showMessage(hint, 6000)
        return
    items = [it for it in clip["items"] if it["track_id"] in track_map]
    if not items:
        return
    conn = sqlite3.connect(tl._project_db())
    try:
        dst_fps = _sequence_fps(conn.cursor(), tl.sequence_id)
    finally:
        conn.close()
    items = [dict(it, track_id=track_map[it["track_id"]])
             for it in convert_items(items, clip["fps"], dst_fps)]
    _paste_core(tl, items, max(0, int(anchor)), 0)


def _paste_core(tl, ok, anchor, skipped, make_tracks=None, label="Paste clips",
                inserted=0):
    # Insert the items ok with their earliest in-point at anchor, in one
    # undo step (label). make_tracks None: each item goes to its own
    # track_id and overwrites what is under it there. make_tracks(cur):
    # called inside the paste's transaction, creates the destination tracks
    # and returns {source track id: new track id}; the new tracks are empty,
    # so nothing is overwritten. inserted: frames make_tracks pushed the
    # later clips by (Paste Insert w/New Tracks), named in the status.
    from PySide6.QtWidgets import QMessageBox
    from keyframe import mem_id_from_db
    project = tl._project_db()
    # Overwrite plan against the current clips on each target track (all
    # types). Items in paste order per track; each existing clip's state is
    # updated as it goes. Left overlap or spanning: out-point trimmed to the
    # paste in. Right overlap: in-point moved to the paste out (start and
    # source in_frame advance, length shrinks; keyframe keys stay put).
    # Fully inside: deleted.
    # BUILD 160: replaced by overwrite_plan below. The old plan cut a clip
    # spanning the paste at the paste in and dropped its right part, and
    # dropped any gap between two pasted items inside one clip; a trimmed
    # keyframe clip kept its keys outside the new range.
    # state = {}      # mem id -> dict(cs, ce, shift, orig_cs, orig_ce, deleted)
    # for it in sorted(ok, key=lambda x: x["offset"]):
    #     s = anchor + it["offset"]
    #     e = s + it["length"]
    #     for c in tl.clips_by_track.get(it["track_id"], []):
    #         st = state.get(c[0])
    #         if st is None:
    #             cs0, ce0 = int(c[2]), int(c[2]) + int(c[3])
    #             st = {"cs": cs0, "ce": ce0, "shift": 0, "orig_cs": cs0,
    #                   "orig_ce": ce0, "deleted": False}
    #         if st["deleted"] or st["ce"] <= s or st["cs"] >= e:
    #             continue
    #         if st["cs"] < s:
    #             st["ce"] = s
    #         elif st["ce"] > e:
    #             st["shift"] += e - st["cs"]
    #             st["cs"] = e
    #         else:
    #             st["deleted"] = True
    #         state[c[0]] = st
    # BUILD 160: per destination track only, each existing clip keeps the
    # parts of its range no pasted item on that track covers (pieces). No
    # pieces: deleted. One piece: trimmed. Two or more (the paste lands
    # inside it): split, the first piece keeps the row, the others are new
    # rows (see apply_overwrite). Never refuses because of overlap.
    state = overwrite_plan(tl, ok, anchor) if make_tracks is None else {}
    # BUILD 106: sequence clips in the paste must pass the self / cycle
    # check against this sequence, else the whole paste is refused
    children = set()
    for it in ok:
        if it["table"] == "clips" and it["row"].get("sequence_id_ref") is not None:
            children.add(int(it["row"]["sequence_id_ref"]))
    if children:
        from sequence_timeline_sql import sequence_contains
        conn = sqlite3.connect(project)
        try:
            bad = [ch for ch in children
                   if sequence_contains(conn.cursor(), ch, int(tl.sequence_id))]
        finally:
            conn.close()
        if bad:
            text = ("Paste refused: a pasted sequence clip would put this "
                    "sequence inside itself (directly or through nesting).")
            tl.status_bar.showMessage(text, 5000)
            QMessageBox.information(tl, "Paste", text)
            return
    from undo import push_undo
    push_undo(project, label)
    ranges = []
    conn = None
    try:
        from sequence_timeline_captions import clip_table_and_id
        conn = sqlite3.connect(project)
        cur = conn.cursor()
        trimmed = deleted = 0
        # BUILD 106: pairs of sequence clips the overwrite deletes
        from sequence_timeline_sql import sequence_pairs_for, prune_sequence_children
        # seq_pairs = sequence_pairs_for(
        #     cur, [m for m, st in state.items() if st["deleted"]])
        seq_pairs = sequence_pairs_for(
            cur, [m for m, st in state.items() if not st["pieces"]])
        # BUILD 160: old trim / delete loop (no split), replaced by
        # apply_overwrite
        # for mem_id, st in state.items():
        #     table, row_id = clip_table_and_id(mem_id)
        #     ranges.append((st["orig_cs"], st["orig_ce"] - 1))
        #     if st["deleted"]:
        #         # Same row delete as the Delete path (no second undo)
        #         cur.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        #         if table == "clips":
        #             from markers import delete_markers
        #             delete_markers(cur, [mem_id])
        #         deleted += 1
        #         continue
        #     trimmed += 1
        #     new_len = max(1, st["ce"] - st["cs"])
        #     if table == "keyframe_clips":
        #         # Keys are absolute frames: they do not move
        #         cur.execute(
        #             "UPDATE keyframe_clips SET in_frame = ?, out_frame = ? WHERE id = ?",
        #             (st["cs"], st["cs"] + new_len, row_id))
        #     elif table == "clips":
        #         cur.execute(
        #             "UPDATE clips SET start_frame = ?, length_frames = ?, "
        #             "in_frame = in_frame + ? WHERE id = ?",
        #             (st["cs"], new_len, st["shift"], row_id))
        #     else:
        #         cur.execute(
        #             f"UPDATE {table} SET start_frame = ?, length_frames = ? WHERE id = ?",
        #             (st["cs"], new_len, row_id))
        trimmed, deleted = apply_overwrite(cur, state, ranges)
        track_map = make_tracks(cur) if make_tracks is not None else {}
        cols_by_table = {}
        groups = {}     # original link_id -> [(table, new row id, mem id)]
        for it in ok:
            table = it["table"]
            if table not in cols_by_table:
                cols_by_table[table] = set(_columns(cur, table))
            cols = cols_by_table[table]
            s = anchor + it["offset"]
            row = dict(it["row"])
            row["track_id"] = track_map.get(it["track_id"], it["track_id"])
            if table == "keyframe_clips":
                row["in_frame"] = s
                row["out_frame"] = s + it["length"]
            else:
                row["start_frame"] = s
            if "sequence_id" in cols:
                row["sequence_id"] = int(tl.sequence_id)
            row["link_id"] = None
            names = [k for k in row if k in cols and k != "id"]
            cur.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES "
                f"({', '.join('?' for _ in names)})",
                [row[k] for k in names])
            new_id = cur.lastrowid
            if table == "keyframe_clips":
                mem = mem_id_from_db(new_id)
                delta = s - it["start"]
                for kd in it["keys"]:
                    kd = dict(kd)
                    ktable = kd.pop("_table", "keyframes")
                    kd["keyframe_clip_id"] = new_id
                    kd["frame"] = int(kd.get("frame", 0)) + delta
                    knames = list(kd)
                    cur.execute(
                        f"INSERT INTO {ktable} ({', '.join(knames)}) VALUES "
                        f"({', '.join('?' for _ in knames)})",
                        [kd[k] for k in knames])
            elif table == "captions":
                mem = -new_id
            else:
                mem = new_id
            if table == "clips" and it.get("markers"):
                from markers import insert_marker_rows
                insert_marker_rows(cur, mem, it["markers"])
            # if it["link_id"] is not None:
            # BUILD 155: a pasted keyframe clip is always unlinked (link_id
            # stays NULL); it does not rejoin its pasted link partners
            # if it["link_id"] is not None and table != "keyframe_clips":
            # BUILD 157: every type (keyframe clips too) rejoins its pasted
            # partners of the same original group
            if it["link_id"] is not None:
                groups.setdefault(it["link_id"], []).append((table, new_id, mem))
            ranges.append((s, s + it["length"] - 1))
        # BUILD 106: pasted sequence clips get their sequence_children row;
        # then drop rows the overwrite left without a referencing clip
        for ch in children:
            cur.execute("INSERT OR IGNORE INTO sequence_children (parent_id, "
                        "child_id) VALUES (?, ?)", (int(tl.sequence_id), ch))
        prune_sequence_children(cur, seq_pairs)
        # New link_id per original group (first pasted member's in-memory
        # id, as Link does); a lone pasted member stays unlinked
        # BUILD 156: no relink at all: every pasted clip stays unlinked
        # (link_id NULL, set on insert above)
        # for members in groups.values():
        #     if len(members) < 2:
        #         continue
        #     new_link = members[0][2]
        #     for table, new_id, _mem in members:
        #         if "link_id" in cols_by_table.get(table, ()):
        #             cur.execute(f"UPDATE {table} SET link_id = ? WHERE id = ?",
        #                         (new_link, new_id))
        # BUILD 157: a group of two or more pasted members (of one original
        # link_id) gets one NEW shared link_id; each original group its own.
        # A lone pasted member (only one copied, or the only one whose track
        # still exists) stays unlinked. The new id is a pasted member's mem
        # id as Link does, preferring a clips row (clips.id is AUTOINCREMENT,
        # never reused), then a keyframe clip, then a caption.
        for members in groups.values():
            if len(members) < 2:
                continue
            pick = ([m for m in members if m[0] == "clips"]
                    or [m for m in members if m[0] == "keyframe_clips"]
                    or members)
            new_link = pick[0][2]
            for table, new_id, _mem in members:
                if "link_id" in cols_by_table.get(table, ()):
                    cur.execute(f"UPDATE {table} SET link_id = ? WHERE id = ?",
                                (new_link, new_id))
        conn.commit()
        conn.close()
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        QMessageBox.warning(tl, "Paste", f"Could not paste: {e}")
        return
    tl._mark_preview_dirty(ranges)
    msg = f"Pasted {len(ok)} item(s) at frame {anchor}"
    if track_map:
        msg += f" on {len(track_map)} new track(s)"
    if inserted:
        msg += f", later clips pushed {inserted} frames"
    if trimmed:
        msg += f", trimmed {trimmed}"
    if deleted:
        msg += f", replaced {deleted}"
    if skipped:
        msg += f", skipped {skipped} (track no longer exists)"
    tl._after_clip_change(msg)


# ---------------------------------------------------------------- BUILD 160
# Paste overwrites: the pasted items replace whatever is in their time range
# on their destination tracks (only tracks that receive a pasted item).


def _subtract(cs, ce, spans):
    # [cs, ce) minus the union of spans [(s, e)], as sorted [(a, b)] pieces
    pieces = [(cs, ce)]
    for s, e in sorted(spans):
        out = []
        for a, b in pieces:
            if e <= a or s >= b:
                out.append((a, b))
                continue
            if a < s:
                out.append((a, s))
            if e < b:
                out.append((e, b))
        pieces = out
    return pieces


def overwrite_plan(tl, ok, anchor):
    # {mem id: {"cs", "ce", "pieces", "link_id"}} for every existing clip a
    # pasted item overlaps on its destination track
    spans = {}
    for it in ok:
        s = anchor + it["offset"]
        spans.setdefault(it["track_id"], []).append((s, s + it["length"]))
    state = {}
    for track_id, sp in spans.items():
        for c in tl.clips_by_track.get(track_id, []):
            cs, ce = int(c[2]), int(c[2]) + int(c[3])
            if not any(s < ce and e > cs for s, e in sp):
                continue
            state[c[0]] = {"cs": cs, "ce": ce, "pieces": _subtract(cs, ce, sp),
                           "link_id": c[8] if len(c) > 8 else None}
    return state


def _copy_row(cur, table, row_id, changes):
    # New row = copy of table row row_id (all columns but id) with changes
    cols = [c for c in _columns(cur, table) if c != "id"]
    cur.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE id = ?", (row_id,))
    vals = dict(zip(cols, cur.fetchone()))
    vals.update(changes)
    cur.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES "
        f"({', '.join('?' for _ in cols)})", [vals[c] for c in cols])
    return cur.lastrowid


def _overwrite_keyframe_clip(cur, mem_id, st):
    # Keyframe clip: cut at every piece edge with the slice path (BUILD
    # 243: every piece keeps a copy of all the keys, so the curve keeps its
    # value on every kept frame), then delete the covered segments and
    # their keys.
    # Returns the kept pieces' mem ids in time order.
    from keyframe import slice_keyframe_clip_db, db_id_from_mem
    cuts = sorted({f for p in st["pieces"] for f in p
                   if st["cs"] < f < st["ce"]})
    segs = [(st["cs"], mem_id)]
    cur_mem = mem_id
    for f in cuts:
        new_mem = slice_keyframe_clip_db(cur, cur_mem, f)
        if new_mem is None:
            continue
        segs.append((f, new_mem))
        cur_mem = new_mem
    kept = []
    for seg_start, seg_mem in segs:
        if any(a <= seg_start < b for a, b in st["pieces"]):
            kept.append(seg_mem)
            continue
        db_id = db_id_from_mem(seg_mem)
        # keyframes / keyframe_border rows (BUILD 222)
        from keyframe import delete_clip_keys
        delete_clip_keys(cur, [db_id])
        cur.execute("DELETE FROM keyframe_clips WHERE id = ?", (db_id,))
    return kept


def apply_overwrite(cur, state, ranges):
    # Run overwrite_plan in the paste's transaction (its one undo step).
    # Trimmed: start / length set, a left trim advances the source in_frame
    # by the same frames. Split: the first piece keeps the row, each other
    # piece is a copy of the row (gain, proxy, sequence ref, text, ...) at
    # its own source in-point; markers on a new piece's source frames move
    # to it, as Slice does. Link like Slice: the first piece keeps the old
    # link_id; the n-th pieces of one original group share a new link_id,
    # an unlinked clip's pieces stay unlinked. Appends each touched clip's
    # old span to ranges. Returns (trimmed, deleted) counts.
    from sequence_timeline_captions import clip_table_and_id
    from keyframe import mem_id_from_db, db_id_from_mem
    trimmed = deleted = 0
    extra = {}      # (old link_id, piece index) -> [(table, row id, mem id)]
    for mem_id, st in state.items():
        table, row_id = clip_table_and_id(mem_id)
        ranges.append((st["cs"], st["ce"] - 1))
        pieces = st["pieces"]
        if not pieces:
            # Same row delete as the Delete path (no second undo)
            if table == "keyframe_clips":
                # keyframes / keyframe_border rows (BUILD 222)
                from keyframe import delete_clip_keys
                delete_clip_keys(cur, [row_id])
            cur.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            if table == "clips":
                from markers import delete_markers
                delete_markers(cur, [mem_id])
            deleted += 1
            continue
        trimmed += 1
        if table == "keyframe_clips":
            kept = _overwrite_keyframe_clip(cur, mem_id, st)
            for n, km in enumerate(kept[1:], 1):
                extra.setdefault((st["link_id"], n), []).append(
                    (table, db_id_from_mem(km), km))
            continue
        a0, b0 = pieces[0]
        if table == "clips":
            cur.execute("SELECT in_frame FROM clips WHERE id = ?", (row_id,))
            in0 = int(cur.fetchone()[0] or 0)
            for n, (a, b) in enumerate(pieces[1:], 1):
                src_in = in0 + (a - st["cs"])
                new_id = _copy_row(cur, "clips", row_id,
                                   {"start_frame": a, "length_frames": b - a,
                                    "in_frame": src_in})
                from markers import move_markers
                move_markers(cur, row_id, new_id, src_in, src_in + (b - a))
                extra.setdefault((st["link_id"], n), []).append(
                    ("clips", new_id, new_id))
            cur.execute(
                "UPDATE clips SET start_frame = ?, length_frames = ?, "
                "in_frame = ? WHERE id = ?",
                (a0, b0 - a0, in0 + (a0 - st["cs"]), row_id))
        else:
            for n, (a, b) in enumerate(pieces[1:], 1):
                new_id = _copy_row(cur, table, row_id,
                                   {"start_frame": a, "length_frames": b - a})
                extra.setdefault((st["link_id"], n), []).append(
                    (table, new_id, -new_id))
            cur.execute(
                f"UPDATE {table} SET start_frame = ?, length_frames = ? WHERE id = ?",
                (a0, b0 - a0, row_id))
    for (old_link, _n), members in extra.items():
        if old_link is None:
            new_link = None
        else:
            pick = ([m for m in members if m[0] == "clips"]
                    or [m for m in members if m[0] == "keyframe_clips"]
                    or members)
            new_link = pick[0][2]
        for table, row_id, _mem in members:
            cur.execute(f"UPDATE {table} SET link_id = ? WHERE id = ?",
                        (new_link, row_id))
    return trimmed, deleted


# ---------------------------------------------------------------- BUILD 288
# Paste w/New Tracks: paste the clipboard (from any sequence of the
# project) into this sequence on new tracks, one per source track that
# holds copied items, created as one block above all existing tracks in the
# source top-to-bottom order (sequence_timeline_tracks.insert_track_copies:
# default names, other settings copied, enabled on, solo off). The earliest
# in-point lands at anchor; links get new ids per original group, keyframe
# clips, captions and markers come along as in Paste. One undo step.
# BUILD 289: Paste Insert w/New Tracks (insert=True) first makes room in
# the same undo step: the pasted span (earliest in to latest out, at the
# target rate) is inserted at anchor on every existing track exactly like
# a Shift+drag insert (sequence_timeline_insert.ripple_insert_db): clips
# spanning anchor are sliced there and everything starting at or after
# anchor moves later by the span.


def _round_frame(x):
    # Nearest whole frame, halves up (also below 0)
    return int(math.floor(x + 0.5))


def convert_items(items, src_fps, dst_fps):
    # Clipboard items re-timed from src_fps to dst_fps sequence frames.
    # Same rate (or a rate unknown): the items unchanged. Else r = dst / src
    # and each item's edges are scaled and rounded (start and end, so pieces
    # that touched still touch), its length is end - start (at least 1);
    # a clip's in_frame, keyframe key frames and marker frames are scaled
    # too, so each item shows the same footage time. Offsets are recomputed
    # from the new starts. Other values (caption pos_x / pos_y, key values)
    # are unchanged.
    from sequence_player import fps_conform_ratio
    r = fps_conform_ratio(dst_fps, src_fps)
    if r is None:
        return items
    out = []
    for it in items:
        s = _round_frame(it["start"] * r)
        e = max(s + 1, _round_frame((it["start"] + it["length"]) * r))
        row = dict(it["row"])
        if it["table"] != "keyframe_clips":
            row["length_frames"] = e - s
        if it["table"] == "clips" and row.get("in_frame") is not None:
            row["in_frame"] = _round_frame(int(row["in_frame"]) * r)
        keys = []
        for kd in it["keys"]:
            kd = dict(kd)
            kd["frame"] = _round_frame(int(kd.get("frame", 0)) * r)
            keys.append(kd)
        marks = [(m[0], _round_frame(int(m[1]) * r)) + tuple(m[2:])
                 for m in it.get("markers") or ()]
        out.append(dict(it, row=row, start=s, length=e - s, keys=keys,
                        markers=marks))
    first = min(it["start"] for it in out)
    for it in out:
        it["offset"] = it["start"] - first
    return out


def paste_new_tracks(tl, anchor, insert=False):
    clip = _clipboard(tl)
    if clip is None or tl._edge_drag is not None:
        return
    src_tracks = [t for t in clip["tracks"]
                  if any(it["track_id"] == t.get("id") for it in clip["items"])]
    have = {t.get("id") for t in src_tracks}
    items = [it for it in clip["items"] if it["track_id"] in have]
    skipped = len(clip["items"]) - len(items)
    if not items:
        return
    conn = sqlite3.connect(tl._project_db())
    try:
        dst_fps = _sequence_fps(conn.cursor(), tl.sequence_id)
    finally:
        conn.close()
    items = convert_items(items, clip["fps"], dst_fps)
    seq_id = int(tl.sequence_id)
    anchor = max(0, int(anchor))
    span = max(it["offset"] + it["length"] for it in items)
    cuts = None
    if insert:
        from sequence_timeline_insert import insert_cuts
        cuts = insert_cuts(tl, anchor)

    def make_tracks(cur):
        from sequence_timeline_tracks import insert_track_copies
        if cuts is not None:
            from sequence_timeline_insert import ripple_insert_db
            ripple_insert_db(tl, cur, anchor, span, cuts)
        copies = insert_track_copies(cur, seq_id, src_tracks)
        cur.execute("SELECT id FROM tracks WHERE sequence_id = ? "
                    "ORDER BY sort_order, id", (seq_id,))
        rest = [r[0] for r in cur.fetchall() if r[0] not in set(copies)]
        for order, tid in enumerate(list(copies) + rest):
            cur.execute("UPDATE tracks SET sort_order = ? WHERE id = ?",
                        (order, tid))
        return dict(zip([t["id"] for t in src_tracks], copies))

    _paste_core(tl, items, anchor, skipped, make_tracks,
                "Paste Insert w/New Tracks" if insert else "Paste w/New Tracks",
                span if insert else 0)

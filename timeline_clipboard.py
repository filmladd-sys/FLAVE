# timeline_clipboard.py
# In-memory Copy / Cut / Paste for the Sequence Timeline. One clipboard per
# (project file, sequence_id); never the system clipboard and never shared
# between sequences. Stores row data, not ids. ASCII only.

import os
import sqlite3

# (abs project path, sequence_id) -> list of item dicts
_CLIPBOARDS = {}


def _key(tl):
    return (os.path.abspath(tl._project_db() or ""), int(tl.sequence_id))


def has_clipboard(tl):
    if tl.sequence_id is None:
        return False
    return bool(_CLIPBOARDS.get(_key(tl)))


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
    # Snapshot the selected set into this sequence's clipboard. Returns the
    # item count (0 = nothing copied, clipboard unchanged).
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
                kcols = _columns(cur, "keyframes")
                cur.execute("SELECT * FROM keyframes WHERE keyframe_clip_id = ?",
                            (row_id,))
                for kr in cur.fetchall():
                    kd = dict(zip(kcols, kr))
                    kd.pop("id", None)
                    kd.pop("keyframe_clip_id", None)
                    keys.append(kd)
            link_id = row.get("link_id")
            row.pop("id", None)
            from markers import marker_rows
            marks = [] if table == "keyframe_clips" else marker_rows(cur, cid)
            items.append({"table": table, "row": row, "track_id": row.get("track_id"),
                          "start": start, "length": length, "link_id": link_id,
                          "keys": keys, "markers": marks})
    finally:
        conn.close()
    if not items:
        return 0
    first = min(it["start"] for it in items)
    for it in items:
        it["offset"] = it["start"] - first
    _CLIPBOARDS[_key(tl)] = items
    return len(items)


def cut_selection(tl):
    # Copy, then the existing Delete path (it takes the undo snapshot)
    if tl._edge_drag is not None:
        return
    n = copy_selection(tl)
    if not n:
        return
    ranges = [(it["start"], it["start"] + it["length"] - 1)
              for it in _CLIPBOARDS[_key(tl)]]
    tl._mark_preview_dirty(ranges)
    tl.delete_selected_clip()


def end_anchor(tl):
    # Latest out-point of any clip in the sequence (0 if none)
    end = 0
    for clips in tl.clips_by_track.values():
        for c in clips:
            end = max(end, int(c[2]) + int(c[3]))
    return end


def paste(tl, anchor, make_room):
    # Paste this sequence's clipboard with its earliest in-point at anchor.
    # Overwrite: existing clips under a pasted item are trimmed, moved in or
    # deleted (see the plan below). make_room is kept for callers; Paste To
    # Tic and Paste to End both overwrite.
    from PySide6.QtWidgets import QMessageBox
    from keyframe import mem_id_from_db
    items = _CLIPBOARDS.get(_key(tl)) if tl.sequence_id is not None else None
    if not items or tl._edge_drag is not None:
        return
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
    # Overwrite plan against the current clips on each target track (all
    # types). Items in paste order per track; each existing clip's state is
    # updated as it goes. Left overlap or spanning: out-point trimmed to the
    # paste in. Right overlap: in-point moved to the paste out (start and
    # source in_frame advance, length shrinks; keyframe keys stay put).
    # Fully inside: deleted.
    state = {}      # mem id -> dict(cs, ce, shift, orig_cs, orig_ce, deleted)
    for it in sorted(ok, key=lambda x: x["offset"]):
        s = anchor + it["offset"]
        e = s + it["length"]
        for c in tl.clips_by_track.get(it["track_id"], []):
            st = state.get(c[0])
            if st is None:
                cs0, ce0 = int(c[2]), int(c[2]) + int(c[3])
                st = {"cs": cs0, "ce": ce0, "shift": 0, "orig_cs": cs0,
                      "orig_ce": ce0, "deleted": False}
            if st["deleted"] or st["ce"] <= s or st["cs"] >= e:
                continue
            if st["cs"] < s:
                st["ce"] = s
            elif st["ce"] > e:
                st["shift"] += e - st["cs"]
                st["cs"] = e
            else:
                st["deleted"] = True
            state[c[0]] = st
    from undo import push_undo
    push_undo(project, "Paste clips")
    ranges = []
    conn = None
    try:
        from sequence_timeline_captions import clip_table_and_id
        conn = sqlite3.connect(project)
        cur = conn.cursor()
        trimmed = deleted = 0
        for mem_id, st in state.items():
            table, row_id = clip_table_and_id(mem_id)
            ranges.append((st["orig_cs"], st["orig_ce"] - 1))
            if st["deleted"]:
                # Same row delete as the Delete path (no second undo)
                cur.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
                from markers import delete_markers
                delete_markers(cur, [mem_id])
                deleted += 1
                continue
            trimmed += 1
            new_len = max(1, st["ce"] - st["cs"])
            if table == "keyframe_clips":
                # Keys are absolute frames: they do not move
                cur.execute(
                    "UPDATE keyframe_clips SET in_frame = ?, out_frame = ? WHERE id = ?",
                    (st["cs"], st["cs"] + new_len, row_id))
            elif table == "clips":
                cur.execute(
                    "UPDATE clips SET start_frame = ?, length_frames = ?, "
                    "in_frame = in_frame + ? WHERE id = ?",
                    (st["cs"], new_len, st["shift"], row_id))
            else:
                cur.execute(
                    f"UPDATE {table} SET start_frame = ?, length_frames = ? WHERE id = ?",
                    (st["cs"], new_len, row_id))
        cols_by_table = {}
        groups = {}     # original link_id -> [(table, new row id, mem id)]
        for it in ok:
            table = it["table"]
            if table not in cols_by_table:
                cols_by_table[table] = set(_columns(cur, table))
            cols = cols_by_table[table]
            s = anchor + it["offset"]
            row = dict(it["row"])
            row["track_id"] = it["track_id"]
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
                    kd["keyframe_clip_id"] = new_id
                    kd["frame"] = int(kd.get("frame", 0)) + delta
                    knames = list(kd)
                    cur.execute(
                        f"INSERT INTO keyframes ({', '.join(knames)}) VALUES "
                        f"({', '.join('?' for _ in knames)})",
                        [kd[k] for k in knames])
            elif table == "captions":
                mem = -new_id
            else:
                mem = new_id
            if it.get("markers"):
                from markers import insert_marker_rows
                insert_marker_rows(cur, mem, it["markers"])
            if it["link_id"] is not None:
                groups.setdefault(it["link_id"], []).append((table, new_id, mem))
            ranges.append((s, s + it["length"] - 1))
        # New link_id per original group (first pasted member's in-memory
        # id, as Link does); a lone pasted member stays unlinked
        for members in groups.values():
            if len(members) < 2:
                continue
            new_link = members[0][2]
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
    if trimmed:
        msg += f", trimmed {trimmed}"
    if deleted:
        msg += f", replaced {deleted}"
    if skipped:
        msg += f", skipped {skipped} (track no longer exists)"
    tl._after_clip_change(msg)

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
            items.append({"table": table, "row": row, "track_id": row.get("track_id"),
                          "start": start, "length": length, "link_id": link_id,
                          "keys": keys})
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
    # make_room (Paste To Tic): an existing clip that starts before a pasted
    # item and overlaps it is trimmed to end at the item's start. An
    # existing clip that starts inside a pasted item's range is a conflict:
    # nothing is pasted or trimmed and the user is told.
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
    # Room and conflicts against the current clips on each target track
    trims = {}      # mem id -> (start, new_length, old_length, track_id)
    conflict = None
    for it in ok:
        s = anchor + it["offset"]
        e = s + it["length"]
        for c in tl.clips_by_track.get(it["track_id"], []):
            cs, ce = int(c[2]), int(c[2]) + int(c[3])
            if s <= cs < e:
                conflict = (it["track_id"], cs)
                break
            if cs < s < ce:
                if not make_room:
                    conflict = (it["track_id"], cs)
                    break
                old = trims.get(c[0])
                new_len = s - cs if old is None else min(old[1], s - cs)
                trims[c[0]] = (cs, new_len, ce - cs, it["track_id"])
        if conflict:
            break
    if conflict:
        QMessageBox.warning(
            tl, "Paste",
            f"Nothing pasted: a clip at frame {conflict[1]} on track "
            f"{conflict[0]} starts inside the pasted range.")
        return
    from undo import push_undo
    push_undo(project, "Paste clips")
    ranges = []
    conn = None
    try:
        from sequence_timeline_captions import clip_table_and_id
        conn = sqlite3.connect(project)
        cur = conn.cursor()
        for mem_id, (cs, new_len, old_len, _tid) in trims.items():
            table, row_id = clip_table_and_id(mem_id)
            if table == "keyframe_clips":
                cur.execute("UPDATE keyframe_clips SET out_frame = ? WHERE id = ?",
                            (cs + new_len, row_id))
            else:
                cur.execute(f"UPDATE {table} SET length_frames = ? WHERE id = ?",
                            (new_len, row_id))
            ranges.append((cs + new_len, cs + old_len - 1))
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
    if trims:
        msg += f", trimmed {len(trims)}"
    if skipped:
        msg += f", skipped {skipped} (track no longer exists)"
    tl._after_clip_change(msg)

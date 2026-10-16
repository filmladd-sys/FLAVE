# markers.py
# Clip markers (Marker table, created only in new_project.py _init_db).
# Marker.clip_id is clips.id of a video / image / audio clip. Marker.frame is in SOURCE terms (BUILD 95): clip in_frame + offset
# at add time, so trims and slices never move it on the picture. Timeline
# frame = clip start + (frame - clip in_frame); markers outside the clip's
# visible range are hidden. Markers are only for video / audio / image
# clips (clips.id > 0); rows with a negative clip_id (old caption markers)
# are skipped everywhere (BUILD 96). Older DBs without Marker load no markers and refuse adds. ASCII only.

import sqlite3

DEFAULT_RGB = (50, 0, 0)
MARK_SIZE = 6
HIT_PX = 3      # click tolerance around a marker line / square
HOVER_LINE_W = 3


def line_width(marker_id, hover_id):
    return HOVER_LINE_W if hover_id is not None and marker_id == hover_id else 1


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
        if int(cid) <= 0:
            continue
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
            if c[0] in exclude_ids or int(c[0]) <= 0:
                continue
            for _mid, mframe, _rgb, _name in markers.get(c[0], ()):
                off = visible_offset(c, mframe)
                if off is not None:
                    out.add(int(c[2]) + off)
    return sorted(out)


def delete_markers(cur, mem_ids):
    # Marker rows of deleted clips (caller's transaction / undo snapshot)
    ids = [int(i) for i in mem_ids if int(i) > 0]
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
    # [(name, frame, r, g, b)] for copy / paste (clips only)
    if int(mem_id) <= 0:
        return []
    try:
        return cur.execute(
            "SELECT name, frame, r, g, b FROM Marker WHERE clip_id = ? ORDER BY id",
            (int(mem_id),)).fetchall()
    except sqlite3.OperationalError:
        return []


def insert_marker_rows(cur, mem_id, rows):
    if int(mem_id) <= 0:
        return
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
    if int(clip[0]) <= 0:
        return None
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


def paint_markers(painter, clip_rect, clip, markers, ppf, hover_id=None):
    # MARK_SIZE square at the clip top plus a full-height line, marker
    # color; the hovered marker's line is HOVER_LINE_W px wide
    from PySide6.QtGui import QColor, QPen
    items = markers.get(clip[0]) if markers and int(clip[0]) > 0 else None
    if not items or ppf <= 0:
        return
    for mid, mframe, rgb, _name in items:
        rel = visible_offset(clip, mframe)
        if rel is None:
            continue
        color = QColor(rgb[0], rgb[1], rgb[2])
        x = int(clip_rect.left() + rel * ppf)
        painter.setPen(QPen(color, line_width(mid, hover_id)))
        painter.drawLine(x, clip_rect.top(), x, clip_rect.bottom())
        painter.fillRect(x - MARK_SIZE // 2, clip_rect.top(), MARK_SIZE, MARK_SIZE,
                         color)


def clamp_to_visible(clip, frame):
    # Timeline frame clamped into the clip's visible range
    start, length = int(clip[2]), max(1, int(clip[3]))
    return max(start, min(start + length - 1, int(frame)))


def hit_test_marker(tl, pos):
    # (clip, marker tuple) for a visible marker under pos (line or square,
    # HIT_PX tolerance), else None. Video / image / audio clips only.
    markers = getattr(tl, "_markers", None)
    if not markers:
        return None
    name_item = tl._clip_image_track_row(pos)
    if name_item is None:
        return None
    x = tl._x_in_clips(pos)
    if x < 0:
        return None
    ppf = float(tl.pixels_per_frame or 0)
    if ppf <= 0:
        return None
    best = None
    from PySide6.QtCore import Qt
    for clip in tl.clips_by_track.get(name_item.data(Qt.UserRole), []):
        if int(clip[0]) <= 0:
            continue
        for m in markers.get(clip[0], ()):
            off = visible_offset(clip, m[1])
            if off is None:
                continue
            mx = (int(clip[2]) + off - tl.view_start_frame) * ppf
            d = abs(x - mx)
            if d <= max(HIT_PX, MARK_SIZE // 2) and (best is None or d < best[0]):
                best = (d, clip, m)
    return None if best is None else (best[1], best[2])


def marker_to_source(clip, timeline_frame):
    # Timeline frame (clamped to the visible range) -> stored source frame
    tf = clamp_to_visible(clip, timeline_frame)
    return _clip_in(clip) + (tf - int(clip[2]))


def delete_marker(project_db, marker_id):
    conn = sqlite3.connect(project_db)
    try:
        conn.execute("DELETE FROM Marker WHERE id = ?", (int(marker_id),))
        conn.commit()
    finally:
        conn.close()


def update_marker(project_db, marker_id, frame, name, rgb):
    conn = sqlite3.connect(project_db)
    try:
        conn.execute("UPDATE Marker SET frame = ?, name = ?, r = ?, g = ?, b = ? "
                     "WHERE id = ?", (int(frame), name,
                                      int(rgb[0]), int(rgb[1]), int(rgb[2]),
                                      int(marker_id)))
        conn.commit()
    finally:
        conn.close()


def edit_marker(tl, clip, marker):
    # Popup: timeline frame, name, R G B (0-255). OK stores the source frame.
    from PySide6.QtWidgets import (QDialog, QFormLayout, QSpinBox, QLineEdit,
                                   QDialogButtonBox, QMessageBox)
    mid, mframe, rgb, name = marker
    start, length = int(clip[2]), max(1, int(clip[3]))
    off = visible_offset(clip, mframe)
    dlg = QDialog(tl)
    dlg.setWindowTitle("Marker")
    form = QFormLayout(dlg)
    frame_spin = QSpinBox()
    frame_spin.setRange(start, start + length - 1)
    frame_spin.setValue(start + (off or 0))
    form.addRow("Frame", frame_spin)
    name_edit = QLineEdit(name or "")
    form.addRow("Name", name_edit)
    spins = []
    for label, v in zip(("R", "G", "B"), rgb):
        s = QSpinBox()
        s.setRange(0, 255)
        s.setValue(int(v))
        form.addRow(label, s)
        spins.append(s)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    form.addRow(buttons)
    result = dlg.exec()
    project_db = tl._project_db()
    if result != QDialog.Accepted:
        return False
    try:
        from undo import push_undo
        push_undo(project_db, "Edit marker")
    except Exception:
        pass
    try:
        update_marker(project_db, mid, marker_to_source(clip, frame_spin.value()),
                      name_edit.text(), [s.value() for s in spins])
    except Exception as e:
        QMessageBox.warning(tl, "Marker", "Could not save marker: %s" % e)
        return False
    tl._after_clip_change("Marker updated")
    return True


def remove_marker(tl, marker):
    # Delete one marker (undo snapshot), then redraw
    from PySide6.QtWidgets import QMessageBox
    project_db = tl._project_db()
    try:
        from undo import push_undo
        push_undo(project_db, "Delete marker")
    except Exception:
        pass
    try:
        delete_marker(project_db, marker[0])
    except Exception as e:
        QMessageBox.warning(tl, "Marker", "Could not delete marker: %s" % e)
        return False
    tl._after_clip_change("Marker deleted")
    return True


def marker_context_menu(tl, hit, pos):
    # Right-click on a marker: Edit / Delete (instead of the clip menu)
    from PySide6.QtWidgets import QMenu
    clip, marker = hit
    menu = QMenu(tl)
    edit_action = menu.addAction("Edit")
    delete_action = menu.addAction("Delete")
    action = menu.exec(tl.table.viewport().mapToGlobal(pos))
    if action is None:
        return
    if action == edit_action:
        edit_marker(tl, clip, marker)
    elif action == delete_action:
        remove_marker(tl, marker)


def _timeline_frame_at(tl, pos):
    return int(round(tl.view_start_frame + tl._x_in_clips(pos) / float(tl.pixels_per_frame)))


def marker_drag_start(tl, hit, pos):
    # Left press on a marker: drag state only (no edit, no clip selection)
    clip, marker = hit
    tl._marker_drag = {"clip": clip, "marker_id": marker[0],
                       "orig_frame": int(marker[1]), "frame": int(marker[1])}


def marker_drag_move(tl, pos):
    # Live: new source frame (clamped to the visible range), no snapping
    drag = tl._marker_drag
    src = marker_to_source(drag["clip"], _timeline_frame_at(tl, pos))
    if src == drag["frame"]:
        return
    drag["frame"] = src
    items = (getattr(tl, "_markers", None) or {}).get(drag["clip"][0], [])
    for i, m in enumerate(items):
        if m[0] == drag["marker_id"]:
            items[i] = (m[0], src, m[2], m[3])
    tl.table.viewport().update()


def marker_drag_release(tl):
    # One write and one undo snapshot per drag; a click without movement
    # changes nothing
    drag = tl._marker_drag
    tl._marker_drag = None
    if drag is None or drag["frame"] == drag["orig_frame"]:
        return False
    project_db = tl._project_db()
    try:
        from undo import push_undo
        push_undo(project_db, "Move marker")
    except Exception:
        pass
    conn = sqlite3.connect(project_db)
    try:
        conn.execute("UPDATE Marker SET frame = ? WHERE id = ?",
                     (int(drag["frame"]), int(drag["marker_id"])))
        conn.commit()
    finally:
        conn.close()
    tl._after_clip_change("Marker moved")
    return True

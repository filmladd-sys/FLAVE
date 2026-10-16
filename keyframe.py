# keyframe.py
# Keyframe clip tables, dialog, load/draw/persist helpers. ASCII only.

import sqlite3

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QPushButton, QMessageBox, QDoubleSpinBox, QApplication,
)

# Stored on keyframe_clips.type. Not enforced beyond storing the string.
KEYFRAME_CLIP_TYPES = ("position X", "position Y", "scale")

# Combo display label -> stored type
KEYFRAME_TYPE_CHOICES = (
    ("X", "position X"),
    ("Y", "position Y"),
    ("Scale", "scale"),
)

# In-memory clip ids: -(KEYFRAME_ID_BASE + db_id) so they never collide
# with caption negatives (-captions.id) or positive clips.id.
KEYFRAME_ID_BASE = 1000000000

TITLE_BAR_H = 15
KEY_MARK_SIZE = 20


def ensure_keyframe_clips_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keyframe_clips ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sequence_id INTEGER NOT NULL, "
        "track_id INTEGER NOT NULL, "
        "type TEXT NOT NULL, "
        "link_id INTEGER, "
        "in_frame INTEGER NOT NULL, "
        "out_frame INTEGER NOT NULL, "
        "name TEXT)"
    )


def ensure_keyframes_table(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS keyframes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "keyframe_clip_id INTEGER NOT NULL, "
        "frame INTEGER NOT NULL, "
        "value REAL NOT NULL)"
    )


def ensure_keyframe_tables(conn):
    ensure_keyframe_clips_table(conn)
    ensure_keyframes_table(conn)


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
    # Position X/Y and scale: two decimal places
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


def scale_percent_for_link(scale_clips, clip_link_id, frame, base_scale=100.0):
    # Linked type-scale keyframe clip whose [in_frame, out_frame) covers
    # frame. base_scale is the clip's existing scale percent (video/image
    # clips have no scale column; callers pass 100). Returns final percent
    # = base * value/100, or None when there is no effect.
    if clip_link_id is None or not scale_clips:
        return None
    for link_id, in_f, out_f, keys in scale_clips:
        if link_id is None or link_id != clip_link_id:
            continue
        if not (int(in_f) <= int(frame) < int(out_f)):
            continue
        value = evaluate_scale(keys, frame)
        if value is None:
            continue
        return float(base_scale) * float(value) / 100.0
    return None


def collect_scale_clips(clips_by_track):
    # Once per snapshot/pass: (link_id, in_frame, out_frame, keys) for every
    # type-scale keyframe clip with a non-null link_id.
    out = []
    if not clips_by_track:
        return out
    for track_clips in clips_by_track.values():
        for c in track_clips:
            if not is_keyframe_clip_id(c[0]):
                continue
            if len(c) <= 10 or c[10] != "scale":
                continue
            link_id = c[8] if len(c) > 8 else None
            if link_id is None:
                continue
            in_f = max(0, int(c[2]))
            out_f = in_f + max(1, int(c[3]))
            keys = list(c[9]) if len(c) > 9 and c[9] else []
            out.append((link_id, in_f, out_f, keys))
    return out


def mark_keyframe_clip_preview_dirty(timeline, clip):
    # Dirty the preview cache over the keyframe clip's [in, out).
    if clip is None or timeline is None:
        return
    in_f = max(0, int(clip[2]))
    length = max(1, int(clip[3]))
    last = in_f + length - 1
    if last < in_f:
        return
    try:
        timeline._mark_preview_dirty([(in_f, last)])
    except Exception:
        pass



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


def paint_keyframe_clip(painter, clip_rect, title, keys=None,
                        clip_start=0, ppf=1.0, stored_type=""):
    # Caption-like body fill, plus a 15 px title bar. Keys: full-height white
    # vertical line, then a sized white square with black border and light
    # grey value text, clipped to the clip rect.
    body = QColor(30, 110, 110)
    painter.fillRect(clip_rect, body)
    h = min(TITLE_BAR_H, max(1, clip_rect.height()))
    title_rect = clip_rect.adjusted(0, 0, 0, -(clip_rect.height() - h))
    painter.fillRect(title_rect, QColor(0, 0, 0))
    painter.setPen(QPen(QColor(255, 255, 255)))
    painter.drawRect(title_rect.adjusted(0, 0, -1, -1))
    if title_rect.width() > 4:
        text = painter.fontMetrics().elidedText(
            " ".join((title or "").split()), Qt.ElideRight,
            max(0, title_rect.width() - 4))
        painter.drawText(title_rect, Qt.AlignCenter, text)
    if not keys or ppf <= 0:
        return
    body_h = max(0, clip_rect.height() - h)
    if body_h <= 0:
        return
    painter.save()
    painter.setClipRect(clip_rect)
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


def persist_keyframe_clip_time(cur, clip):
    # Memory start/length -> in_frame/out_frame. Keyframes store absolute
    # sequence frames; shift them by the same delta so they stay with the clip.
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
    if delta != 0:
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
    # Name + type (X / Y / Scale) + Add
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
    # Preview dirty over the new clip range (scale takes effect once linked)
    try:
        timeline._mark_preview_dirty([(in_frame, out_frame - 1)])
    except Exception:
        pass
    timeline._after_clip_change(
        "Added keyframe clip at frame %d" % in_frame)


class AddKeyDialog(QDialog):
    # One numeric value labeled with the clip type (X / Y / Scale)
    def __init__(self, parent, type_label, initial=None, title="Add Key"):
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("%s:" % (type_label or "Value")))
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(2)
        self.spin.setRange(-1000000000.0, 1000000000.0)
        self.spin.setSingleStep(0.1)
        if initial is not None:
            self.spin.setValue(float(initial))
        row.addWidget(self.spin)
        layout.addLayout(row)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(ok_btn)

    def value(self):
        return float(self.spin.value())


def clip_type_from_db(cur, db_id):
    cur.execute("SELECT type FROM keyframe_clips WHERE id = ?", (int(db_id),))
    row = cur.fetchone()
    return (row[0] if row else "") or ""


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
    return 0.0


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
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Add Key", "Could not read clip: %s" % e)
        return
    initial = default_key_value(timeline, stored_type)
    dlg = AddKeyDialog(timeline, type_label, initial=initial)
    if dlg.exec() != QDialog.Accepted:
        return
    value = dlg.value()
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        ensure_keyframe_tables(conn)
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
    dlg = AddKeyDialog(timeline, type_label, initial=value, title="Edit Key")
    if dlg.exec() != QDialog.Accepted:
        return
    new_val = dlg.value()
    project_db = timeline._project_db()
    if not project_db:
        return
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

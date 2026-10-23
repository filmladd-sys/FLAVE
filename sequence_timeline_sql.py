# sequence_timeline_sql.py
# Database and SQL helpers for the Sequence Timeline (no window class here).
# Window methods take the SequenceTimeline instance as self. ASCII only.

import math
import os
import sqlite3
from PySide6.QtWidgets import QMessageBox, QInputDialog, QDialog, QTableWidgetItem
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from sequence_timeline_ruler import CLIPS_COL
from sequence_timeline_image import IMAGE_EXTENSIONS
from sequence_timeline_video import VIDEO_EXTENSIONS, out_frame_for
from sequence_timeline_audio import AUDIO_EXTENSIONS
from sequence_timeline_captions import (
    is_caption_id, clip_table_and_id, load_captions,
)
from keyframe import (
    load_keyframe_clips, is_keyframe_clip_id,
    keyframe_range_overlaps, persist_keyframe_clip_time,
)
from text_clip import is_text_clip_id, text_mem_id

EDGE_HIT_PX = 6
OUT_ICON_MIN_PX = 4   # smallest visible out-icon block on narrow clips
EMPTY_LENGTH_SECONDS = 60  # ruler length for an empty sequence; no maximum
DEFAULT_FPS = 29.97
DEFAULT_TRACK_HEIGHT = 64
# BUILD 126: collapsible tracks. The triangle column is LOGICAL column 3,
# moved to VISUAL position 0 (far left) with QHeaderView.moveSection, so
# Name (0), Type (1) and Clips (CLIPS_COL = 2) keep their logical indexes
# everywhere (items, delegates, sectionViewportPosition, columnAt).
COLLAPSE_COL = 3
COLLAPSE_COL_WIDTH = 18
COLLAPSED_ROW_HEIGHT = 16
ROLE_COLLAPSED = Qt.UserRole + 5     # on the Name item: 0 / 1
ROLE_OPEN_HEIGHT = Qt.UserRole + 6   # on the Name item: height_px when open


# Track types whose clips can be dropped, moved, trimmed and toggled.
# keyframe is not listed: footage drops are rejected like caption.
CLIP_TRACK_TYPES = ("image", "video", "audio")
# Track types whose clips can be hit, dragged, trimmed, sliced and deleted.
# Caption / keyframe / text clips use negative in-memory ids (separate
# ranges). "text" is the WYSIWYG Text track (BUILD 308, text_clip.py).
EDIT_TRACK_TYPES = CLIP_TRACK_TYPES + ("caption", "keyframe", "text")
TRACK_FOOTAGE_EXTENSIONS = {
    "image": IMAGE_EXTENSIONS,
    "video": VIDEO_EXTENSIONS,
    "audio": AUDIO_EXTENSIONS,
}


# Global settings store (globalsettings.db): owned by global_settings.py
from global_settings import _global_db_path, _get_global_setting, _set_global_setting


def _read_open_ids():
    value = _get_global_setting("open_timelines", "") or ""
    ids = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids


def _write_open_ids(ids):
    _set_global_setting("open_timelines", ",".join(str(i) for i in ids))


def _add_open_id(sequence_id):
    ids = _read_open_ids()
    if sequence_id not in ids:
        ids.append(sequence_id)
        _write_open_ids(ids)


def _remove_open_id(sequence_id):
    ids = _read_open_ids()
    if sequence_id in ids:
        ids.remove(sequence_id)
        _write_open_ids(ids)


def get_open_timeline_ids():
    return _read_open_ids()


# Open Sequence Players: global setting open_players, comma list of sequence ids
def _read_open_player_ids():
    value = _get_global_setting("open_players", "") or ""
    ids = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids


def _write_open_player_ids(ids):
    _set_global_setting("open_players", ",".join(str(i) for i in ids))


def _add_open_player_id(sequence_id):
    ids = _read_open_player_ids()
    if sequence_id not in ids:
        ids.append(sequence_id)
        _write_open_player_ids(ids)


def _remove_open_player_id(sequence_id):
    ids = _read_open_player_ids()
    if sequence_id in ids:
        ids.remove(sequence_id)
        _write_open_player_ids(ids)


def get_open_player_ids():
    return _read_open_player_ids()


def sequence_exists(project_db, sequence_id):
    if not project_db or not os.path.isfile(project_db):
        return False
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM sequences WHERE id = ?", (sequence_id,))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return False
    return row is not None


def _load_sequence_name(sequence_id):
    project_db = _get_global_setting("current_project", "")
    if not project_db or not os.path.isfile(project_db):
        return None
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sequences WHERE id = ?", (sequence_id,))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    return row[0] if row and row[0] else None


def _latest_clip_end(project_db, sequence_id, fps=DEFAULT_FPS):
    # End time in seconds of the latest clip on this sequence, or None.
    # end frame = start_frame + length_frames; seconds = end frame / fps.
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(COALESCE(c.start_frame, 0) + COALESCE(c.length_frames, 0)) "
            "FROM clips c JOIN tracks t ON t.id = c.track_id WHERE t.sequence_id = ?",
            (sequence_id,)
        )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    if row and row[0] is not None and row[0] > 0:
        return float(row[0]) / (fps if fps and fps > 0 else DEFAULT_FPS)
    return None


def sequence_length_seconds(sequence_id):
    # Greatest of: latest clip end, empty default. No upper limit.
    candidates = [EMPTY_LENGTH_SECONDS]
    project_db = _get_global_setting("current_project", "")
    if project_db and os.path.isfile(project_db):
        clip_end = _latest_clip_end(project_db, sequence_id, _load_sequence_fps(sequence_id))
        if clip_end:
            candidates.append(clip_end)
    return max(candidates)


def _load_sequence_fps(sequence_id):
    project_db = _get_global_setting("current_project", "")
    if not project_db or not os.path.isfile(project_db):
        return DEFAULT_FPS
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT fps FROM sequences WHERE id = ?", (sequence_id,))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return DEFAULT_FPS
    if row and row[0] and row[0] > 0:
        return float(row[0])
    return DEFAULT_FPS


def _add_track(self, track_type, title, name=None, language="", bg_color=None,
               caption_type=None, undo=True):
    # undo=False: the caller already pushed one undo step (+ Video/Audio)
    project_db = self._project_db()
    if not project_db:
        QMessageBox.warning(self, title, "No project open")
        return
    if name is None:
        from track_names import ask_track_name
        name, ok = ask_track_name(self, title, track_type, project_db,
                                  self.sequence_id)
        name = (name or "").strip()
        if not ok or not name:
            return
    from undo import push_undo
    if undo:
        push_undo(project_db, "Add track")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tracks WHERE sequence_id = ?",
            (self.sequence_id,)
        )
        sort_order = cur.fetchone()[0]
        if bg_color:
            cur.execute(
                "INSERT INTO tracks (sequence_id, name, track_type, sort_order, "
                "language, bg_color) VALUES (?, ?, ?, ?, ?, ?)",
                (self.sequence_id, name, track_type, sort_order,
                 language or "", bg_color)
            )
        else:
            cur.execute(
                "INSERT INTO tracks (sequence_id, name, track_type, sort_order, "
                "language) VALUES (?, ?, ?, ?, ?)",
                (self.sequence_id, name, track_type, sort_order, language or "")
            )
        if caption_type:
            # BUILD 101: caption stream type (column from new_project._init_db)
            cur.execute("UPDATE tracks SET caption_type = ? WHERE id = ?",
                        (caption_type, cur.lastrowid))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, title, f"Could not add track: {e}")
        return
    self.refresh()
    self.status_bar.showMessage(f"Added {track_type} track: {name}", 3000)


def refresh(self):
    self.length_seconds = sequence_length_seconds(self.sequence_id)
    self.table.setRowCount(0)
    project_db = self._project_db()
    if not project_db or not os.path.isfile(project_db):
        return
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        # caption_type (BUILD 101; NULL shows as WebVTT), collapsed (BUILD 126)
        cur.execute(
            "SELECT id, name, track_type, COALESCE(enabled, 1), COALESCE(solo, 0), "
            "COALESCE(height_px, 64), COALESCE(bg_color, ''), "
            "COALESCE(language, ''), caption_type, COALESCE(collapsed, 0) "
            "FROM tracks WHERE sequence_id = ? ORDER BY sort_order, id",
            (self.sequence_id,))
        rows = cur.fetchall()
        cur.execute(
            "SELECT c.id, c.track_id, c.footage_id, c.start_frame, c.length_frames, "
            "COALESCE(c.enabled, 1), COALESCE(c.solo, 0), c.in_frame, "
            "COALESCE(f.filename, f.original_path, ''), c.link_id, t.track_type "
            "FROM clips c JOIN tracks t ON t.id = c.track_id "
            "LEFT JOIN footage f ON f.id = c.footage_id "
            "WHERE t.sequence_id = ? ORDER BY c.start_frame, c.id",
            (self.sequence_id,)
        )
        clips = {}
        for (clip_id, track_id, footage_id, start_frame, length_frames,
             clip_enabled, clip_solo, in_frame, filename, link_id,
             track_type) in cur.fetchall():
            # Image (still) clips may have an in-point below source 0 (a
            # left trim has no source floor for them; markers stay in
            # source terms). Video / audio clips and sequence clips
            # (footage_id NULL) never go below 0.
            in_f = int(in_frame or 0)
            if (track_type or "").lower() != "image" or footage_id is None:
                in_f = max(0, in_f)
            # List, not tuple: edge drags update start and length in place
            clips.setdefault(track_id, []).append(
                [clip_id, footage_id, max(0, int(start_frame or 0)), int(length_frames or 1),
                 os.path.basename(filename or ""), int(clip_enabled), int(clip_solo),
                 in_f, link_id]
            )
        # BUILD 105: sequence clips (footage_id NULL, sequence_id_ref set):
        # title is the child sequence name; clip id -> child id kept apart
        self._seq_refs = load_sequence_refs(cur, self.sequence_id)
        for track_clips in clips.values():
            for c in track_clips:
                ref = self._seq_refs.get(c[0])
                if ref is not None:
                    c[4] = ref[1]
        load_captions(cur, self.sequence_id, clips)
        load_keyframe_clips(cur, self.sequence_id, clips)
        # BUILD 308: WYSIWYG Text clips (text_clip.py)
        from text_clip import load_text_clips
        load_text_clips(cur, self.sequence_id, clips)
        for track_clips in clips.values():
            track_clips.sort(key=lambda c: (c[2], c[0]))
        self.clips_by_track = clips
        # BUILD 165: clip gain (dB) for the title bar and waveform scale
        from clip_gain import load_clip_gains
        self._clip_gains = load_clip_gains(cur, self.sequence_id)
        # BUILD 303: clip speed percent by clip id (only != 100), for
        # labels, trims, icons and the player snapshot (clip_speed.py)
        from clip_speed import load_clip_speeds
        self._clip_speeds = load_clip_speeds(cur, self.sequence_id)
        # Frame rate conform by footage id, for the clip icons
        # (sequence_timeline_video.icon_source_frame)
        from sequence_player import load_fps_conform
        self._fps_conform = load_fps_conform(conn, getattr(self, "fps", None))
        # BUILD 102: player caption overlay rebuilds from the new rows
        self._caption_snapshot = None
        conn.close()
    except Exception as e:
        self.status_bar.showMessage(f"DB error: {e}")
        return
    self._filling = True
    try:
        self.table.setRowCount(len(rows))
        read_only = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        dim = QColor(130, 130, 130)
        for i, (track_id, name, track_type, enabled, solo, height_px, bg_color,
                language, caption_type, collapsed) in enumerate(rows):
            name_item = QTableWidgetItem(name or "")
            name_item.setData(Qt.UserRole, track_id)
            name_item.setData(Qt.UserRole + 1, int(enabled))
            name_item.setData(Qt.UserRole + 2, int(solo))
            display_bg = bg_color or ""
            if ((track_type or "").lower() == "keyframe"
                    and display_bg.lower() == "#403050"):
                display_bg = ""
            name_item.setData(Qt.UserRole + 3, display_bg)
            name_item.setData(Qt.UserRole + 4, track_type or "")
            name_item.setFlags(read_only | Qt.ItemIsEditable)
            type_text = ((track_type or "") + (" [S]" if solo else "")
                         + ("" if enabled else " [d]"))
            if (track_type or "").lower() == "caption":
                from sequence_timeline_captions import caption_type_text
                type_text = caption_type_text(
                    language, caption_type,
                    (" [S]" if solo else "") + ("" if enabled else " [d]"))
            elif (track_type or "").lower() == "text":
                from text_clip import TEXT_TRACK_LABEL
                type_text = TEXT_TRACK_LABEL + type_text[len(track_type):]
            type_item = QTableWidgetItem(type_text)
            type_item.setFlags(read_only)
            clips_item = QTableWidgetItem("")
            clips_item.setFlags(read_only)
            if not enabled:
                name_item.setForeground(dim)
                type_item.setForeground(dim)
            name_item.setTextAlignment(Qt.AlignCenter)
            type_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 0, name_item)
            self.table.setItem(i, 1, type_item)
            self.table.setItem(i, CLIPS_COL, clips_item)
            # BUILD 126: triangle cell (painted by ClipDelegate)
            coll_item = QTableWidgetItem("")
            coll_item.setFlags(read_only)
            coll_item.setToolTip("Collapse / expand track")
            self.table.setItem(i, COLLAPSE_COL, coll_item)
            try:
                row_h = int(height_px)
            except (TypeError, ValueError):
                row_h = DEFAULT_TRACK_HEIGHT
            row_h = row_h if row_h > 0 else DEFAULT_TRACK_HEIGHT
            is_coll = 1 if collapsed else 0
            name_item.setData(ROLE_COLLAPSED, is_coll)
            name_item.setData(ROLE_OPEN_HEIGHT, row_h)
            self.table.setRowHeight(i, track_row_height(row_h, is_coll))
    finally:
        self._filling = False
    self._update_clips_width()


def _save_track_display(self, track_id, height_px, bg_color):
    # bg_color "" means transparent
    try:
        height_px = int(height_px)
    except (TypeError, ValueError):
        height_px = DEFAULT_TRACK_HEIGHT
    if height_px <= 0:
        height_px = DEFAULT_TRACK_HEIGHT
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute(
            "UPDATE tracks SET height_px = ?, bg_color = ? WHERE id = ?",
            (height_px, bg_color or "", track_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Track Display Properties", f"Could not save track: {e}")
        return
    self._icon_cache.clear()
    self._clear_icon_misses()
    self.refresh()
    self.status_bar.showMessage("Saved track display properties", 3000)


def _move_track(self, track_id, direction):
    project_db = self._project_db()
    from undo import push_undo
    push_undo(project_db, "Reorder track")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM tracks WHERE sequence_id = ? ORDER BY sort_order, id",
            (self.sequence_id,)
        )
        ids = [row[0] for row in cur.fetchall()]
        if track_id not in ids:
            conn.close()
            return
        i = ids.index(track_id)
        j = i + direction
        if j < 0 or j >= len(ids):
            conn.close()
            return
        ids[i], ids[j] = ids[j], ids[i]
        # Renumber so equal or missing sort_order values cannot block a swap
        for order, tid in enumerate(ids):
            cur.execute("UPDATE tracks SET sort_order = ? WHERE id = ?", (order, tid))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Move Track", f"Could not move track: {e}")
        return
    self._after_track_change()


def reorder_track_ids(ids, track_id, where):
    # BUILD 124: ids in sort_order (index 0 = top row, smallest sort_order;
    # 'Up' = toward index 0). where 'top' / 'bottom': track_id moved to that
    # end, the others keep their relative order. Returns the new list, or
    # None if track_id is missing or already there.
    if track_id not in ids:
        return None
    rest = [t for t in ids if t != track_id]
    new = [track_id] + rest if where == "top" else rest + [track_id]
    return None if new == list(ids) else new


def _move_track_to(self, track_id, where):
    # BUILD 124: Move > To Top / To Bottom. Same undo label, renumbering
    # (0..n-1) and refresh as _move_track (Up / Down); a no-op (already at
    # that end) writes nothing and pushes no undo step.
    project_db = self._project_db()
    try:
        conn = sqlite3.connect(project_db)
        try:
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM tracks WHERE sequence_id = ? ORDER BY sort_order, id",
                (self.sequence_id,)).fetchall()]
        finally:
            conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Move Track", f"Could not move track: {e}")
        return
    new = reorder_track_ids(ids, track_id, where)
    if new is None:
        return
    from undo import push_undo
    push_undo(project_db, "Reorder track")
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        for order, tid in enumerate(new):
            cur.execute("UPDATE tracks SET sort_order = ? WHERE id = ?", (order, tid))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Move Track", f"Could not move track: {e}")
        return
    self._after_track_change()


def track_row_height(open_height, collapsed):
    # BUILD 126: the one row-height rule. Every y position (clip paint,
    # hit tests, key boxes, markers, drop preview, header cells) comes from
    # QTableView rowViewportPosition / rowHeight / rowAt / visualRect, which
    # follow these per-row heights.
    if collapsed:
        return COLLAPSED_ROW_HEIGHT
    try:
        h = int(open_height)
    except (TypeError, ValueError):
        h = DEFAULT_TRACK_HEIGHT
    return h if h > 0 else DEFAULT_TRACK_HEIGHT


def _toggle_track_flag(self, track_id, column):
    # BUILD 126: 'collapsed' too (no undo, like enabled / solo)
    if column not in ("enabled", "solo", "collapsed"):
        return
    default = 1 if column == "enabled" else 0
    project_db = self._project_db()
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute(
            f"UPDATE tracks SET {column} = CASE WHEN COALESCE({column}, ?) = 0 "
            "THEN 1 ELSE 0 END WHERE id = ?",
            (default, track_id)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Track", f"Could not update track: {e}")
        return
    self._after_track_change()


def drop_footage(self, footage_id, row, x_in_clips):
    # Insert a clip: image footage on image tracks (one second), video on
    # video (full length, fitted to the room, BUILD 301), audio on audio
    # (full length)
    if row < 0 or x_in_clips < 0:
        return False
    name_item = self.table.item(row, 0)
    if name_item is None:
        return False
    track_id = name_item.data(Qt.UserRole)
    track_type = (name_item.data(Qt.UserRole + 4) or "").lower()
    if track_id is None or track_type not in CLIP_TRACK_TYPES:
        self.status_bar.showMessage("Drop footage on an image, video or audio track", 3000)
        return False
    project_db = self._project_db()
    if not project_db or not os.path.isfile(project_db):
        return False
    start_frame = max(0, self.view_start_frame + int(x_in_clips / self.pixels_per_frame))
    length_frames = max(1, int(round(self.fps)))
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        cur.execute("SELECT filename, original_path FROM footage WHERE id = ?", (footage_id,))
        frow = cur.fetchone()
        # Image on image tracks, video on video tracks, audio on audio tracks
        wanted = TRACK_FOOTAGE_EXTENSIONS[track_type]
        matches = bool(frow) and any(
            v and str(v).lower().endswith(wanted) for v in frow
        )
        if not matches:
            conn.close()
            self.status_bar.showMessage(
                f"Only {track_type} footage can go on a {track_type} track", 3000
            )
            return False
        if track_type == "audio":
            # Audio clips span the footage: peaks duration, else the
            # footage duration column, else 1 second
            length_frames = self._audio_length_frames(cur, footage_id)
        # Video with stored peaks and an audio track in this sequence:
        # also add a linked audio clip (BUILD 109: nearest audio track below
        # the video, else above). BUILD 301: the video (and its linked
        # audio) goes in at the footage's full length (footage_length_frames)
        # unless the room up to the next clip is shorter; then both fill
        # only that room, the tightest across the video and audio tracks
        # (fit_video_drop), so they stay in sync.
        audio_track_id = None
        if track_type == "video":
            peak_seconds = self._peaks_seconds(cur, footage_id)
            linked = peak_seconds > 0 and self._first_audio_track_id(cur) is not None
            full = footage_length_frames(cur, footage_id, self.fps, peak_seconds)
            length_frames, audio_track_id, refused = fit_video_drop(
                self, cur, track_id, start_frame, full, linked,
                lambda tid, st: next_clip_room(cur, tid, st))
            # Refuse the drop (insert nothing) if the drop frame is inside a
            # clip on the video track, or no audio track is free there
            if refused:
                conn.close()
                self.status_bar.showMessage(
                    f"Drop refused: overlaps a clip on the {refused} track", 3000)
                return False
        # All drops start at source frame 0
        from undo import push_undo
        push_undo(project_db, "Add clip")
        cur.execute(
            "INSERT INTO clips (track_id, footage_id, start_frame, length_frames, in_frame) "
            "VALUES (?, ?, ?, ?, 0)",
            (track_id, footage_id, start_frame, length_frames)
        )
        if track_type in ("video", "image"):
            # New video / image clip takes the footage Proxy Default
            cur.execute(
                "UPDATE clips SET proxy_percent = (SELECT COALESCE(proxy_percent, 0) "
                "FROM footage WHERE id = ?) WHERE id = ?",
                (footage_id, cur.lastrowid))
        if audio_track_id is not None:
            # link_id is the video clip id, shared by both clips
            link_id = cur.lastrowid
            cur.execute("UPDATE clips SET link_id = ? WHERE id = ?", (link_id, link_id))
            cur.execute(
                "INSERT INTO clips (track_id, footage_id, start_frame, length_frames, "
                "in_frame, link_id) VALUES (?, ?, ?, ?, 0, ?)",
                (audio_track_id, footage_id, start_frame, length_frames, link_id)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Add Clip", f"Could not add clip: {e}")
        return False
    self.refresh()
    if track_type == "video":
        # Dropped video clips start at source frame 0
        self._generate_out_icon(footage_id, track_id, out_frame_for(0, length_frames))
    self.table.viewport().update()
    self.ruler.viewport().update()
    self._refresh_sequence_manager()
    self.status_bar.showMessage(f"Added clip at frame {start_frame}", 3000)
    return True


def _range_overlaps(self, cur, track_id, start, length):
    # True if [start, start + length) overlaps any clip on track_id
    ttype = self._track_type(track_id)
    if ttype == "keyframe":
        return keyframe_range_overlaps(cur, track_id, start, length)
    table = {"caption": "captions", "text": "text_clips"}.get(ttype, "clips")
    cur.execute(
        f"SELECT 1 FROM {table} WHERE track_id = ? "
        "AND start_frame < ? AND start_frame + length_frames > ? LIMIT 1",
        (track_id, start + length, start)
    )
    return cur.fetchone() is not None


def delete_selected_clip(self):
    # Delete key: remove every selected clip; unless selection solo, also
    # every clip sharing a link_id with a selected clip
    if not self._selected_clip_ids or self._edge_drag is not None:
        return
    ids = delete_clip_ids(self)
    from undo import push_undo
    push_undo(self._project_db(), "Delete clip")
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        delete_clip_ids_db(cur, ids)
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Delete Clip", f"Could not delete clip: {e}")
        return
    self._selected_clip_ids = set()
    self._selection_order = []
    self._selection_solo = False
    self._after_clip_change(f"Deleted {len(ids)} clip(s)")


def delete_clip_ids(self):
    # Clip ids the Delete key / Delete / Ripple Delete remove: every
    # selected clip; unless selection solo, also every clip sharing a
    # link_id with a selected clip
    ids = set(self._selected_clip_ids)
    if not self._selection_solo:
        for c in self._selected_clips():
            for m in self._linked_clips(c):
                ids.add(m[0])
    return ids


def delete_clip_ids_db(cur, ids):
    # The database part of Delete (BUILD 245: shared with Ripple Delete).
    # No commit / undo here: the caller owns the transaction and undo step.
    # BUILD 106: (parent, child) of deleted sequence clips, read first
    seq_pairs = sequence_pairs_for(cur, ids)
    for cid in ids:
        table, row_id = clip_table_and_id(cid)
        cur.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        if table == "keyframe_clips":
            # BUILD 222: its keys go with it (keyframes or, for a
            # border clip, keyframe_border rows; undo restores both)
            from keyframe import delete_clip_keys
            delete_clip_keys(cur, [row_id])
    prune_sequence_children(cur, seq_pairs)
    # Their markers go too (same undo snapshot)
    from markers import delete_markers
    delete_markers(cur, ids)


def ripple_delete_clips(self):
    # BUILD 245 / 246: clip menu Ripple Delete. Removes the same clips as
    # Delete (delete_clip_ids / delete_clip_ids_db), then closes the empty
    # time the deleted set leaves across ALL tracks of the sequence:
    # S / E = earliest start / latest end of the deleted clips (footage,
    # caption and keyframe clips). G1 = E; G0 = the earliest frame >= S
    # where [G0, G1) is empty on every track, i.e. just after the latest
    # end of any remaining clip (any track or type) that overlaps [S, E).
    # Empty space before S is not closed. Every remaining clip starting at
    # or after G1, plus its link partners, moves left by G1 - G0, clamped by
    # the shared ripple rules (ripple_moving: nothing before 0, no overlap
    # with a clip that is not moving). Keyframe clips move with their keys,
    # markers are source-relative and follow their clips. One undo step
    # ("Ripple delete"); with nothing to close it is a plain delete.
    if not self._selected_clip_ids or self._edge_drag is not None:
        return
    ids = delete_clip_ids(self)
    all_clips = [c for clips in self.clips_by_track.values() for c in clips]
    gone = [c for c in all_clips if c[0] in ids]
    if not gone:
        return
    span_s = min(int(c[2]) for c in gone)
    span_e = max(int(c[2]) + int(c[3]) for c in gone)
    kept = [c for c in all_clips if c[0] not in ids]
    g0 = span_s
    for c in kept:
        c_end = int(c[2]) + int(c[3])
        if int(c[2]) < span_e and c_end > span_s and c_end > g0:
            g0 = c_end
    g1 = span_e
    want = g0 - g1          # negative = frames to close
    moving = {}
    delta = 0
    if want < 0:
        right = [c for c in kept if int(c[2]) >= g1]
        if right:
            moving, delta = ripple_moving(self, right, want, ids)
            if delta >= 0:
                moving, delta = {}, 0
    from undo import push_undo
    push_undo(self._project_db(), "Ripple delete")
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        delete_clip_ids_db(cur, ids)
        for c in moving.values():
            c[2] = c[2] + delta
            if is_keyframe_clip_id(c[0]):
                persist_keyframe_clip_time(cur, c)
                continue
            table, row_id = clip_table_and_id(c[0])
            cur.execute(f"UPDATE {table} SET start_frame = ? WHERE id = ?",
                        (c[2], row_id))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Ripple Delete", f"Could not ripple delete: {e}")
        self.refresh()
        return
    self._selected_clip_ids = set()
    self._selection_order = []
    self._selection_solo = False
    if want >= 0:
        msg = (f"Ripple Delete: deleted {len(ids)} clip(s); nothing to close "
               "(a remaining clip covers the end of the span)")
    elif not moving:
        msg = f"Ripple Delete: deleted {len(ids)} clip(s); no clips to move"
    else:
        msg = (f"Ripple Delete: deleted {len(ids)} clip(s), moved "
               f"{len(moving)} by {delta} frames")
        if delta != want:
            msg += f" (limited from {want}: a clip was in the way)"
    self._after_clip_change(msg)


def slice_clip(self, clip, x_in_clips):
    # Split clip in two at the mark x (clips-column x where the menu
    # opened). Linked partners are sliced at the same timeline frame; the
    # left pieces keep the old link_id, the right pieces share a new one.
    # BUILD 132: keyframe clips (the clip itself or a linked partner) are
    # split by keyframe.slice_keyframe_clip_db (BUILD 243: each piece keeps
    # a copy of all the keys) instead of the footage path; captions' and
    # keyframe clips' right pieces join the new right link too, and so do
    # linked captions (BUILD 242) and keyframe clips (BUILD 243) wholly
    # right of the cut. Still one push_undo for the whole cut.
    start, length = clip[2], clip[3]
    if length < 2:
        return
    mark = self.view_start_frame + x_in_clips / self.pixels_per_frame
    if not (start <= mark < start + length):
        return
    # Clamp inside the clip: never on its first frame, and the right part
    # keeps at least one frame
    split_frame = max(start + 1, min(start + length - 1, int(mark)))
    if self._selection_solo and clip[0] in self._selected_clip_ids:
        # Selection solo: only this clip; its right piece stays unlinked
        linked = False
        members = [clip]
    else:
        linked = clip[8] is not None if len(clip) > 8 else False
        members = [clip] + self._linked_clips(clip)
    from undo import push_undo
    push_undo(self._project_db(), "Slice clip")
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        slice_members_db(self, cur, members, split_frame, linked)
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Slice Clip", f"Could not slice clip: {e}")
        return
    self._after_clip_change(f"Sliced clip at frame {split_frame}")


def slice_members_db(self, cur, members, split_frame, linked):
    # The database part of slice_clip: split each member that spans
    # split_frame into a left piece (keeps its row) and a right piece (new
    # row). linked True gives the right pieces a new shared link_id. No
    # commit / undo here: the caller owns the transaction and undo step
    # (slice_clip, and the Shift+drag insert in sequence_timeline_insert.py).
    right_ids = []
    right_other = []   # BUILD 132: right caption / keyframe mem ids
    for c in members:
        c_start, c_len, c_in = c[2], c[3], c[7]
        if not (c_start < split_frame < c_start + c_len):
            # BUILD 242: a linked caption wholly right of the cut (start at
            # or after split_frame) joins the right pieces' new link;
            # captions ending at or before it stay with the left pieces.
            # BUILD 243: linked keyframe clips the same way (their keys
            # are untouched)
            # BUILD 308: linked text clips the same way as captions
            if (linked and (is_caption_id(c[0]) or is_keyframe_clip_id(c[0])
                            or is_text_clip_id(c[0]))
                    and c_start >= split_frame):
                right_other.append(c[0])
            continue
        track_id = self._clip_track_id(c)
        if track_id is None:
            continue
        left_length = split_frame - c_start
        right_length = c_len - left_length
        # BUILD 303: at clip speed s the right piece starts on the source
        # frame the left piece would show at split_frame (in + floor(rel *
        # s / 100), clip_speed.source_offset); both pieces keep s
        from clip_speed import source_offset, source_span, copy_speed, row_speed
        speed = row_speed(cur, *clip_table_and_id(c[0]))
        right_in = c_in + source_offset(split_frame - c_start, speed)
        if is_keyframe_clip_id(c[0]):
            from keyframe import slice_keyframe_clip_db
            new_mem = slice_keyframe_clip_db(cur, c[0], split_frame)
            if new_mem is not None:
                right_other.append(new_mem)
            continue
        if is_caption_id(c[0]):
            # Caption: two rows, same text, font, position and scale
            cap_id = -c[0]
            cur.execute("UPDATE captions SET length_frames = ? WHERE id = ?",
                        (left_length, cap_id))
            cur.execute(
                "INSERT INTO captions (sequence_id, track_id, start_frame, "
                "length_frames, text, font_family, font_size, pos_x, pos_y, scale) "
                "SELECT sequence_id, track_id, ?, ?, text, font_family, font_size, "
                "pos_x, pos_y, scale FROM captions WHERE id = ?",
                (split_frame, right_length, cap_id)
            )
            copy_speed(cur, "captions", cap_id, cur.lastrowid)
            right_other.append(-cur.lastrowid)
            continue
        if is_text_clip_id(c[0]):
            # BUILD 308: text clip: two rows, same text and style (the right
            # row starts unlinked; the link step below may join it)
            from timeline_clipboard import _copy_row
            _t, row_id = clip_table_and_id(c[0])
            cur.execute("UPDATE text_clips SET length_frames = ? WHERE id = ?",
                        (left_length, row_id))
            new_row = _copy_row(cur, "text_clips", row_id,
                                {"start_frame": split_frame,
                                 "length_frames": right_length,
                                 "link_id": None})
            right_other.append(text_mem_id(new_row))
            continue
        cur.execute("UPDATE clips SET length_frames = ? WHERE id = ?",
                    (left_length, c[0]))
        cur.execute(
            "INSERT INTO clips (track_id, footage_id, start_frame, length_frames, "
            "in_frame, enabled, solo) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (track_id, c[1], split_frame, right_length, right_in,
             1 if c[5] else 0, 1 if c[6] else 0)
        )
        right_ids.append(cur.lastrowid)
        # Markers on the right piece's source frames move to it
        from markers import move_markers
        move_markers(cur, c[0], right_ids[-1], right_in,
                     right_in + source_span(right_length, speed))
        copy_speed(cur, "clips", c[0], right_ids[-1])
        # Right piece keeps the clip gain and proxy percent
        cur.execute(
            "UPDATE clips SET gain_db = (SELECT gain_db FROM clips WHERE id = ?) "
            "WHERE id = ?", (c[0], right_ids[-1]))
        cur.execute(
            "UPDATE clips SET proxy_percent = (SELECT proxy_percent FROM clips "
            "WHERE id = ?) WHERE id = ?", (c[0], right_ids[-1]))
        # BUILD 105: right piece of a sequence clip stays a sequence clip
        cur.execute(
            "UPDATE clips SET sequence_id_ref = (SELECT sequence_id_ref "
            "FROM clips WHERE id = ?) WHERE id = ?", (c[0], right_ids[-1]))
    if linked and (right_ids or right_other):
        # New link_id for the right pieces: the first right clip's id,
        # else (no footage piece) the first right caption / keyframe
        # piece's mem id, the same values link_selected_clips uses
        new_link = right_ids[0] if right_ids else right_other[0]
        cur.executemany("UPDATE clips SET link_id = ? WHERE id = ?",
                        [(new_link, rid) for rid in right_ids])
        from keyframe import persist_keyframe_link_id
        from sequence_timeline_captions import persist_caption_link_id
        for mid in right_other:
            if is_keyframe_clip_id(mid):
                persist_keyframe_link_id(cur, mid, new_link)
            else:
                persist_caption_link_id(cur, mid, new_link)
    elif right_other:
        # Unlinked cut: the new keyframe piece copied the old link_id
        # (NULL for an unlinked clip, or a solo-sliced member's group);
        # it stays out of any group like the footage right piece
        from keyframe import persist_keyframe_link_id
        for mid in right_other:
            if is_keyframe_clip_id(mid):
                persist_keyframe_link_id(cur, mid, None)


def ripple_gap(self, track_id, click_frame):
    # BUILD 297: the gap Ripple Delete at click_frame on one track, without
    # changing anything. The gap runs from the end of the previous clip on
    # the track (or 0) to the start of the next one; the clips from that
    # one on move left, with their linked partners, by the gap clamped by
    # ripple_moving. Returns (moving {id: clip}, delta < 0), or (None,
    # status message) when there is nothing to close. The track menu
    # greys out its Ripple Delete on (None, ...).
    track_clips = self.clips_by_track.get(track_id, [])
    wall = 0
    for c in track_clips:
        end = c[2] + c[3]
        if end <= click_frame and end > wall:
            wall = end
    right = sorted([c for c in track_clips if c[2] >= click_frame],
                   key=lambda c: c[2])
    if not right:
        return None, "Ripple Delete: no clips to the right"
    moving, delta = ripple_moving(self, right, wall - right[0][2])
    if delta >= 0:
        return None, "Ripple Delete: no gap to close"
    return moving, delta


def ripple_delete(self, track_id, click_frame):
    # Close the gap at click_frame on one track by moving the clips to its
    # right left until the first one hits the previous clip's end (or 0)
    # (ripple_gap). Linked partners (same link_id) move by the same delta.
    # Footage, caption and keyframe clips (BUILD 297: the keyframe track
    # menu lists it again) all move; one undo step.
    moving, delta = ripple_gap(self, track_id, click_frame)
    if moving is None:
        self.status_bar.showMessage(delta, 3000)
        return
    from undo import push_undo
    push_undo(self._project_db(), "Ripple delete")
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        for c in moving.values():
            if is_keyframe_clip_id(c[0]):
                c[2] = c[2] + delta
                persist_keyframe_clip_time(cur, c)
                continue
            table, row_id = clip_table_and_id(c[0])
            cur.execute(f"UPDATE {table} SET start_frame = ? WHERE id = ?",
                        (c[2] + delta, row_id))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Ripple Delete", f"Could not ripple delete: {e}")
        return
    self._after_clip_change(
        f"Ripple Delete: moved {len(moving)} clip(s) by {delta} frames")


def ripple_moving(self, right, delta, gone=()):
    # Ripple rules shared by both Ripple Deletes. Moving set: the right
    # side clips plus their link partners (same link_id, any track), minus
    # gone (clips being deleted). delta (negative) is clamped: nothing
    # before 0, no overlap with a clip that is not moving (gone clips do
    # not block). Returns (moving {id: clip}, int delta).
    moving = {}
    for c in right:
        moving[c[0]] = c
        for p in self._linked_clips(c):
            if p[0] not in gone:
                moving[p[0]] = p
    clip_track = {}
    for tid, clips in self.clips_by_track.items():
        for c in clips:
            clip_track[c[0]] = tid
    for c in moving.values():
        delta = max(delta, -c[2])
        for n in self.clips_by_track.get(clip_track.get(c[0]), []):
            if n[0] in moving or n[0] in gone:
                continue
            n_end = n[2] + n[3]
            if n[2] < c[2] and n_end > c[2] + delta:
                delta = max(delta, n_end - c[2])
    return moving, int(delta)


def _toggle_clip_flag(self, clip_id, column):
    # Flip one clip's enabled or solo flag; other clips' solos are untouched
    if (column not in ("enabled", "solo") or is_caption_id(clip_id)
            or is_text_clip_id(clip_id)):
        return
    default = 1 if column == "enabled" else 0
    try:
        conn = sqlite3.connect(self._project_db())
        cur = conn.cursor()
        cur.execute(
            f"UPDATE clips SET {column} = CASE WHEN COALESCE({column}, {default}) = 1 "
            f"THEN 0 ELSE 1 END WHERE id = ?",
            (clip_id,)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Clip", f"Could not update clip: {e}")
        return
    self.refresh()
    self.table.viewport().update()


# ---------------------------------------------------------------- BUILD 105
# Sequence clips: a clips row with sequence_id_ref = nested child sequence
# (footage_id NULL), plus a sequence_children (parent, child) row. UI only:
# the preview composite and audio mix skip them (no footage path).

SEQUENCE_MIME = "application/x-ladd-sequence-id"


def sequence_id_from_mime(mime):
    if mime is None or not mime.hasFormat(SEQUENCE_MIME):
        return None
    try:
        value = bytes(mime.data(SEQUENCE_MIME)).decode("ascii").strip()
    except Exception:
        return None
    return int(value) if value.isdigit() else None


def load_sequence_refs(cur, sequence_id):
    # {clip_id: (child_sequence_id, child_name)} for this sequence's
    # sequence clips
    rows = cur.execute(
        "SELECT c.id, c.sequence_id_ref, COALESCE(s.name, '') FROM clips c "
        "JOIN tracks t ON t.id = c.track_id "
        "LEFT JOIN sequences s ON s.id = c.sequence_id_ref "
        "WHERE t.sequence_id = ? AND c.sequence_id_ref IS NOT NULL",
        (int(sequence_id),)).fetchall()
    return {int(cid): (int(ref), name or ("Sequence #%d" % int(ref)))
            for cid, ref, name in rows}


def sequence_contains(cur, ancestor, target):
    # True if target is ancestor or nested inside it at any depth
    # (walks sequence_children; cycle-safe)
    seen = set()
    stack = [int(ancestor)]
    while stack:
        sid = stack.pop()
        if sid == int(target):
            return True
        if sid in seen:
            continue
        seen.add(sid)
        for (child,) in cur.execute(
                "SELECT child_id FROM sequence_children WHERE parent_id = ?",
                (sid,)).fetchall():
            stack.append(int(child))
    return False


def child_sequence_info(cur, child_id):
    # (length_frames, has_video, has_audio) of a sequence. Length is the
    # Sequence Manager's duration (BUILD 100 sequence_stats: latest
    # out-point of clips, captions and keyframe clips). has_video: any clip
    # on a video or image track; has_audio: any clip on an audio track.
    from sequence_manager import sequence_stats
    st = sequence_stats(cur).get(int(child_id), {})
    frames = int(st.get("frames", 0) or 0)
    kinds = set()
    for (tt,) in cur.execute(
            "SELECT DISTINCT LOWER(COALESCE(t.track_type, '')) FROM clips c "
            "JOIN tracks t ON t.id = c.track_id WHERE t.sequence_id = ?",
            (int(child_id),)).fetchall():
        kinds.add(tt)
    return frames, bool(kinds & {"video", "image"}), "audio" in kinds


def _first_free_track(self, cur, kinds, start, length):
    # First track (sort order) of one of kinds in this sequence with no
    # clip in [start, start + length), or None
    marks = ",".join("?" for _ in kinds)
    rows = cur.execute(
        "SELECT id FROM tracks WHERE sequence_id = ? AND LOWER(track_type) IN (%s) "
        "ORDER BY sort_order, id" % marks,
        [self.sequence_id] + list(kinds)).fetchall()
    for (tid,) in rows:
        if not self._range_overlaps(cur, tid, start, length):
            return tid
    return None


def source_cap_frames(seconds, fps):
    # BUILD 301: right-trim cap on a footage clip's source out-point
    # (in_frame + length_frames; edit points are sequence frames, i.e.
    # sequence time, also under fps conform - sequence_player
    # clip_source_frame). The reported duration rounded up to a whole
    # second (12.3 s -> 13 s, 12.0 s -> 12 s) times the sequence fps,
    # rounded down. None (no cap) when the duration or fps is missing / 0.
    try:
        sec, f = float(seconds or 0), float(fps or 0)
    except (TypeError, ValueError):
        return None
    if sec <= 0 or f <= 0:
        return None
    return max(1, int(math.floor(math.ceil(sec - 1e-6) * f + 1e-6)))


def footage_cap_map(cur, footage_ids, fps):
    # {footage_id: source_cap_frames} for footage with a duration > 0
    out = {}
    ids = sorted({int(i) for i in footage_ids if i is not None})
    if not ids:
        return out
    try:
        rows = cur.execute(
            "SELECT id, duration FROM footage WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids).fetchall()
    except sqlite3.Error:
        return out
    for fid, dur in rows:
        cap = source_cap_frames(dur, fps)
        if cap is not None:
            out[int(fid)] = cap
    return out


def footage_length_frames(cur, footage_id, fps, peak_seconds=0.0):
    # BUILD 301: full length of a video drop in sequence frames:
    # round(footage.duration * fps), else the stored peaks duration, else
    # one second
    seconds = 0.0
    try:
        row = cur.execute("SELECT duration FROM footage WHERE id = ?",
                          (footage_id,)).fetchone()
        seconds = float(row[0]) if row and row[0] else 0.0
    except (sqlite3.Error, TypeError, ValueError):
        seconds = 0.0
    if seconds <= 0:
        seconds = float(peak_seconds or 0)
    if seconds <= 0:
        seconds = 1.0
    return max(1, int(round(seconds * float(fps or 1))))


def next_clip_room(cur, track_id, start):
    # Frames from start to the next clip starting at or after it on
    # track_id (clips table), None if there is none
    row = cur.execute(
        "SELECT MIN(start_frame) FROM clips WHERE track_id = ? AND start_frame >= ?",
        (track_id, start)).fetchone()
    return None if not row or row[0] is None else int(row[0]) - int(start)


def fit_video_drop(self, cur, track_id, start, full, linked, room):
    # BUILD 301: (length, audio_track_id, refused) for a video drop of
    # full frames at start. refused "video": start is inside a clip on the
    # video track; "audio": linked and no audio track is free at start.
    # length is full, cut to room(track_id, start) (frames to the next
    # clip, None = open); a linked audio clip goes on the nearest audio
    # track (below first) with room for that length, else the nearest one
    # free at start, and the length is cut to its room too. Used by
    # drop_footage and its drag preview (sequence_timeline_droppreview).
    if self._range_overlaps(cur, track_id, start, 1):
        return full, None, "video"
    length = full
    r = room(track_id, start)
    if r is not None:
        length = max(1, min(length, r))
    audio = None
    if linked:
        audio = _nearest_free_track(self, cur, track_id, ("audio",), start,
                                    length, prefer_below=True)
        if audio is None:
            audio = _nearest_free_track(self, cur, track_id, ("audio",), start,
                                        1, prefer_below=True)
            if audio is None:
                return length, None, "audio"
            r = room(audio, start)
            if r is not None:
                length = max(1, min(length, r))
    return length, audio, None


def _nearest_free_track(self, cur, target_id, kinds, start, length, prefer_below):
    # BUILD 109: on-screen stack order is ORDER BY sort_order, id (table
    # row 0 = top = smallest sort_order). Nearest track of kinds with no
    # clip in [start, start + length): scanning down from the target first
    # if prefer_below, else up first; then the other direction. None if
    # every track of that kind is busy (or there is none).
    rows = cur.execute(
        "SELECT id, LOWER(COALESCE(track_type, '')) FROM tracks "
        "WHERE sequence_id = ? ORDER BY sort_order, id",
        (self.sequence_id,)).fetchall()
    order = [int(r[0]) for r in rows]
    kind_of = {int(r[0]): r[1] for r in rows}
    if int(target_id) not in order:
        return None
    i = order.index(int(target_id))
    below = order[i + 1:]
    above = list(reversed(order[:i]))
    for group in ((below, above) if prefer_below else (above, below)):
        for tid in group:
            if kind_of[tid] in kinds and not self._range_overlaps(cur, tid, start, length):
                return tid
    return None


def _seq_refuse(self, text):
    self.status_bar.showMessage(text, 5000)
    QMessageBox.information(self, "Add Sequence Clip", text)
    return False


def drop_sequence(self, child_id, row, x_in_clips):
    # Same row / frame math as drop_footage. Video or image target: video
    # clip there (+ linked audio clip on the first audio track with room if
    # the child has audio). Audio target: audio clip there (+ linked video
    # clip on the first video track with room if the child has video).
    if row < 0 or x_in_clips < 0:
        return False
    name_item = self.table.item(row, 0)
    if name_item is None:
        return False
    track_id = name_item.data(Qt.UserRole)
    track_type = (name_item.data(Qt.UserRole + 4) or "").lower()
    if track_id is None or track_type not in CLIP_TRACK_TYPES:
        self.status_bar.showMessage("Drop a sequence on an image, video or audio track", 3000)
        return False
    project_db = self._project_db()
    if not project_db or not os.path.isfile(project_db):
        return False
    child_id = int(child_id)
    if child_id == int(self.sequence_id):
        return _seq_refuse(self, "A sequence cannot be placed inside itself.")
    start_frame = max(0, self.view_start_frame + int(x_in_clips / self.pixels_per_frame))
    try:
        conn = sqlite3.connect(project_db)
        cur = conn.cursor()
        if sequence_contains(cur, child_id, self.sequence_id):
            conn.close()
            return _seq_refuse(
                self, "Refused: this sequence is already inside the dropped "
                      "sequence, so nesting it would make a loop.")
        length, has_video, has_audio = child_sequence_info(cur, child_id)
        if length <= 0:
            conn.close()
            return _seq_refuse(self, "Refused: the dropped sequence is empty (length 0).")
        # The target track takes the clip of its own kind if the child has
        # that kind; the other kind goes on the first track of that kind
        # with room. A child with only one kind gets only that clip (on
        # the first free track of that kind if the target is the other).
        target_kind = "audio" if track_type == "audio" else "video"
        has = {"video": has_video, "audio": has_audio}
        kinds_of = {"video": ("video",), "audio": ("audio",)}
        if not has_video and not has_audio:
            conn.close()
            return _seq_refuse(self, "Refused: the dropped sequence has no video "
                                     "or audio clips.")
        placements = []          # (track_id, kind), main clip first
        for kind in (target_kind, "audio" if target_kind == "video" else "video"):
            if not has[kind]:
                continue
            if kind == target_kind:
                if self._range_overlaps(cur, track_id, start_frame, length):
                    conn.close()
                    return _seq_refuse(self, "Drop refused: overlaps a clip on the "
                                             "%s track" % track_type)
                placements.append((track_id, kind))
                continue
            n_tracks = cur.execute(
                "SELECT COUNT(*) FROM tracks WHERE sequence_id = ? AND "
                "LOWER(track_type) = ?", (self.sequence_id, kind)).fetchone()[0]
            if not n_tracks:
                if not placements and not has[target_kind]:
                    conn.close()
                    return _seq_refuse(self, "Refused: the dropped sequence has only "
                                             "%s and this sequence has no %s track"
                                       % (kind, kind))
                continue        # no track of that kind: skip, like footage
            # BUILD 109: linked audio goes below a video target (else
            # above); linked video goes above an audio target (else below)
            tid = _nearest_free_track(self, cur, track_id, kinds_of[kind],
                                      start_frame, length,
                                      prefer_below=(kind == "audio"))
            if tid is None:
                conn.close()
                return _seq_refuse(self, "Drop refused: no %s track has room at "
                                         "that span" % kind)
            placements.append((tid, kind))
        main_kind = placements[0][1]
        from undo import push_undo
        push_undo(project_db, "Add sequence clip")
        new_ids = []
        for tid, _kind in placements:
            cur.execute(
                "INSERT INTO clips (track_id, footage_id, start_frame, length_frames, "
                "in_frame, sequence_id_ref) VALUES (?, NULL, ?, ?, 0, ?)",
                (tid, start_frame, length, child_id))
            new_ids.append(cur.lastrowid)
        if len(new_ids) > 1:
            # Pair shares link_id = first clip id, like a footage drop
            cur.executemany("UPDATE clips SET link_id = ? WHERE id = ?",
                            [(new_ids[0], i) for i in new_ids])
        cur.execute(
            "INSERT OR IGNORE INTO sequence_children (parent_id, child_id) VALUES (?, ?)",
            (int(self.sequence_id), child_id))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Add Sequence Clip", f"Could not add sequence clip: {e}")
        return False
    self.refresh()
    self.table.viewport().update()
    self.ruler.viewport().update()
    self._refresh_sequence_manager()
    self.status_bar.showMessage(
        f"Added sequence clip at frame {start_frame} ({main_kind}"
        + (" + linked clip" if len(placements) > 1 else "") + ")", 3000)
    return True


# ---------------------------------------------------------------- BUILD 106
# sequence_children follows the sequence clips: a delete removes the
# (parent, child) row once no sequence clip in parent still references child.

def sequence_pairs_for(cur, mem_ids):
    # {(parent_sequence_id, child_sequence_id)} for the sequence clips among
    # mem_ids (positive clips ids only). Call BEFORE deleting the rows.
    ids = [int(i) for i in mem_ids if i is not None and int(i) > 0]
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    rows = cur.execute(
        "SELECT t.sequence_id, c.sequence_id_ref FROM clips c "
        "JOIN tracks t ON t.id = c.track_id "
        "WHERE c.sequence_id_ref IS NOT NULL AND c.id IN (%s)" % marks,
        ids).fetchall()
    return {(int(p), int(ch)) for p, ch in rows}


def prune_sequence_children(cur, pairs):
    # Delete each (parent, child) row with no remaining referencing clip
    removed = 0
    for parent, child in pairs:
        left = cur.execute(
            "SELECT 1 FROM clips c JOIN tracks t ON t.id = c.track_id "
            "WHERE t.sequence_id = ? AND c.sequence_id_ref = ? LIMIT 1",
            (parent, child)).fetchone()
        if left is None:
            cur.execute("DELETE FROM sequence_children WHERE parent_id = ? "
                        "AND child_id = ?", (parent, child))
            removed += cur.rowcount
    return removed


# ---------------------------------------------------------------- BUILD 107
# Delete Track (track header menu): the tracks row and everything on it, in
# the caller's transaction / one undo snapshot.

def delete_track_rows(cur, track_id):
    # Deletes clips (+ their markers and BUILD 106 sequence_children
    # pruning), keyframe clips (+ their keys), captions, text clips and the
    # track row.
    # Returns [(first, last)] frame ranges of what was removed (for preview
    # dirty marking). Linked partners on other tracks are not touched.
    tid = int(track_id)
    ranges = []

    def _rows(sql, args=()):
        return cur.execute(sql, args).fetchall()
    clip_rows = _rows("SELECT id, start_frame, length_frames FROM clips "
                     "WHERE track_id = ?", (tid,))
    clip_ids = [int(r[0]) for r in clip_rows]
    ranges += [(int(s), int(s) + max(1, int(n)) - 1) for _i, s, n in clip_rows]
    if clip_ids:
        pairs = sequence_pairs_for(cur, clip_ids)
        cur.execute("DELETE FROM clips WHERE track_id = ?", (tid,))
        prune_sequence_children(cur, pairs)
        from markers import delete_markers
        delete_markers(cur, clip_ids)
    kf_rows = _rows("SELECT id, in_frame, out_frame FROM keyframe_clips "
                   "WHERE track_id = ?", (tid,))
    if kf_rows:
        marks = ",".join("?" for _ in kf_rows)
        # keyframes / keyframe_border rows (BUILD 222)
        from keyframe import delete_clip_keys
        delete_clip_keys(cur, [int(r[0]) for r in kf_rows])
        cur.execute("DELETE FROM keyframe_clips WHERE track_id = ?", (tid,))
        ranges += [(int(a), max(int(a), int(b) - 1)) for _i, a, b in kf_rows]
    cap_rows = _rows("SELECT start_frame, length_frames FROM captions "
                    "WHERE track_id = ?", (tid,))
    if cap_rows:
        cur.execute("DELETE FROM captions WHERE track_id = ?", (tid,))
    # BUILD 308: WYSIWYG Text clips (picture: their ranges go dirty)
    txt_rows = _rows("SELECT start_frame, length_frames FROM text_clips "
                    "WHERE track_id = ?", (tid,))
    if txt_rows:
        cur.execute("DELETE FROM text_clips WHERE track_id = ?", (tid,))
        ranges += [(int(s), int(s) + max(1, int(n)) - 1) for s, n in txt_rows]
    cur.execute("DELETE FROM tracks WHERE id = ?", (tid,))
    return ranges


def delete_track(self, track_id):
    # Header menu Delete Track: confirm (default No), then one undo snapshot
    project_db = self._project_db()
    if not project_db:
        return
    answer = QMessageBox.question(
        self, "Delete Track", "Are you sure?",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if answer != QMessageBox.Yes:
        return
    from undo import push_undo
    push_undo(project_db, "Delete track")
    try:
        conn = sqlite3.connect(project_db)
        try:
            ranges = delete_track_rows(conn.cursor(), track_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as e:
        QMessageBox.warning(self, "Delete Track", f"Could not delete track: {e}")
        return
    self._selected_clip_ids = set()
    self._selection_order = []
    self._selection_solo = False
    if ranges:
        # Picture under the removed clips changes (audio re-mixes on Play
        # by its signature)
        self._mark_preview_dirty(ranges)
    # Reload: timeline, player, preview worker, Sequence Manager counts
    self._after_clip_change("Deleted track")

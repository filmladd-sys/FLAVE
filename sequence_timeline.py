# sequence_timeline.py
# Sequence Timeline window for Ladd Video Editor
# Track table with a frame ruler and scrubber in the Clips header.
# ASCII only.

import math
import os
import sqlite3
from sequence_timeline_ruler import (
    CLIPS_COL, RULER_HEIGHT, ZOOM_MIN_PPF, ZOOM_MAX_PPF, ZOOM_STEP,
    RulerHeader, clamp_zoom, ruler_label_interval, ruler_label_text,
    PlayheadOverlay,
)
from sequence_timeline_image import IMAGE_EXTENSIONS, _add_image_track
from sequence_timeline_keyframe import _add_keyframe_track
from sequence_timeline_video import (
    VIDEO_EXTENSIONS, out_frame_for, video_out_pixmap,
    _add_video_track, _generate_out_icon, _clip_is_video,
)
from sequence_timeline_audio import (
    AUDIO_EXTENSIONS, _draw_waveform, _add_audio_track, _peaks_seconds,
    _first_audio_track_id, _audio_length_frames, peaks_for,
)
from sequence_timeline_captions import (
    CAPTION_LANGUAGES, ensure_captions_table, is_caption_id, clip_table_and_id,
    load_captions, paint_caption_bar, caption_label,
    _add_caption_track, _sequence_size, add_caption, edit_caption,
)
from sequence_timeline_sql import (
    EDGE_HIT_PX, OUT_ICON_MIN_PX, EMPTY_LENGTH_SECONDS, DEFAULT_FPS,
    DEFAULT_TRACK_HEIGHT, CLIP_TRACK_TYPES, EDIT_TRACK_TYPES, TRACK_FOOTAGE_EXTENSIONS,
    _global_db_path, _get_global_setting, _set_global_setting, _read_open_ids,
    _write_open_ids, _add_open_id, _remove_open_id, get_open_timeline_ids,
    _write_open_player_ids, get_open_player_ids,
    sequence_exists, _load_sequence_name, ensure_tracks_table, ensure_clips_table,
    _load_sequence_duration, _latest_clip_end, sequence_length_seconds, _load_sequence_fps,
    _add_track, refresh, _save_track_display, _move_track,
    _toggle_track_flag, drop_footage, _range_overlaps, delete_selected_clip,
    slice_clip, ripple_delete, _toggle_clip_flag,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QStatusBar, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QInputDialog, QAbstractItemView, QMenu, QApplication,
    QStyledItemDelegate, QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QCheckBox, QLineEdit, QLabel, QColorDialog, QDialogButtonBox
)
from PySide6.QtCore import Qt, QRect, QEvent, QTimer
from PySide6.QtGui import (
    QAction, QPainter, QColor, QPen, QShortcut, QKeySequence, QPixmap
)


class ClipDelegate(QStyledItemDelegate):
    # Waveform drawing lives in sequence_timeline_audio.py
    _draw_waveform = _draw_waveform

    # Paints track background and clips in the Clips column
    def __init__(self, timeline):
        super().__init__(timeline.table)
        self.timeline = timeline

    def paint(self, painter, option, index):
        if index.column() != CLIPS_COL:
            super().paint(painter, option, index)
            return
        tl = self.timeline
        rect = option.rect
        painter.save()
        painter.setClipRect(rect)
        name_item = tl.table.item(index.row(), 0)
        track_id = name_item.data(Qt.UserRole) if name_item is not None else None
        track_type = (name_item.data(Qt.UserRole + 4) or "").lower() if name_item is not None else ""
        bg = name_item.data(Qt.UserRole + 3) if name_item is not None else ""
        # BUILD 52 stored #403050 on keyframe tracks; treat like caption/video default (none)
        if bg and track_type == "keyframe" and bg.lower() == "#403050":
            bg = ""
        if bg:
            color = QColor(bg)
            if color.isValid():
                painter.fillRect(rect, color)
        ppf = tl.pixels_per_frame
        for clip in tl.clips_by_track.get(track_id, []):
            (clip_id, footage_id, start_frame, length_frames, filename,
             clip_enabled, clip_solo, in_frame) = clip[:8]
            # Clip rect is the time span only, whatever the icon sizes
            x = int(rect.left() + (start_frame - tl.view_start_frame) * ppf)
            w = max(1, int(length_frames * ppf))
            if x + w < rect.left() or x > rect.right():
                continue
            clip_rect = QRect(x, rect.top(), w, rect.height())
            # Disabled clips draw dimmer
            painter.setOpacity(1.0 if clip_enabled else 0.4)
            if track_type == "keyframe":
                from keyframe import paint_keyframe_clip
                keys = clip[9] if len(clip) > 9 else []
                ktype = clip[10] if len(clip) > 10 else ""
                paint_keyframe_clip(
                    painter, clip_rect, filename or "", keys=keys,
                    clip_start=start_frame, ppf=ppf, stored_type=ktype,
                    linked=(len(clip) > 8 and clip[8] is not None))
                if clip_id in tl._selected_clip_ids:
                    painter.setPen(QColor(255, 255, 160))
                else:
                    painter.setPen(QColor(160, 180, 220))
                painter.drawRect(clip_rect.adjusted(0, 0, -1, -1))
                painter.setOpacity(1.0)
                continue
            if track_type == "caption":
                paint_caption_bar(painter, clip_rect)
            else:
                painter.fillRect(clip_rect, QColor(70, 90, 130))
            text_rect = QRect(clip_rect)
            if track_type == "caption":
                # Caption: plain bar with the text, no icon, no waveform
                pass
            elif track_type == "audio":
                # Audio clips: waveform from stored peaks, no icons
                self._draw_waveform(painter, tl, clip_rect, footage_id,
                                    in_frame, length_frames)
            else:
                # Video clips show the source frame at their left edge; stills use 0
                icon_frame = in_frame if track_type == "video" else 0
                # No grabs mid-drag; generate on release
                pix = tl.icon_pixmap(footage_id, rect.height(), icon_frame,
                                     generate=tl._edge_drag is None, clip_id=clip_id)
                has_in = pix is not None and not pix.isNull()
                # Out icon: video reads storage only (never generated in paint);
                # stills reuse the in icon; missing draws black
                if track_type == "video":
                    out_pix = video_out_pixmap(tl, footage_id, rect.height(),
                                               in_frame, length_frames, clip_id)
                else:
                    out_pix = pix
                has_out = out_pix is not None and not out_pix.isNull()
                in_full_w = pix.width() if has_in else 0
                if has_out:
                    out_full_w = out_pix.width()
                else:
                    out_full_w = in_full_w if has_in else rect.height()
                # Room for icons: the whole clip; the edge handles overlap the
                # icons on both sides
                avail = w
                if in_full_w + out_full_w <= avail:
                    in_w, out_w = in_full_w, out_full_w
                else:
                    # No room for both: skip the out icon entirely (no black
                    # placeholder); the in icon shows its left part
                    out_w = 0
                    in_w = min(in_full_w, avail)
                if has_in and in_w > 0:
                    painter.drawPixmap(x, rect.top(), pix, 0, 0, in_w, pix.height())
                    text_rect.setLeft(x + in_w)
                if out_w > 0:
                    # Flush right: icon's right edge on the clip's right edge
                    out_x = x + w - out_w
                    if has_out:
                        painter.drawPixmap(out_x, rect.top(), out_pix,
                                           out_pix.width() - out_w, 0, out_w, out_pix.height())
                    else:
                        # Black only for a real missing frame: there is room,
                        # but the out frame is past the footage end or its
                        # icon was requested and does not exist
                        painter.fillRect(QRect(out_x, rect.top(), out_w, rect.height()),
                                         QColor(0, 0, 0))
                    text_rect.setRight(out_x - 1)
            # Markers: square at the top plus a line (video / image / audio only)
            from markers import paint_markers
            if track_type != "caption":
                paint_markers(painter, clip_rect, clip, getattr(tl, "_markers", None), ppf,
                              getattr(tl, "_hover_marker_id", None))
            # Outline last so the clip's full time span, handle included, is visible
            # Selected clip (last click or clip menu) gets a light yellow border
            if clip_id in tl._selected_clip_ids:
                painter.setPen(QColor(255, 255, 160))
            else:
                painter.setPen(QColor(160, 180, 220))
            painter.drawRect(clip_rect.adjusted(0, 0, -1, -1))
            if text_rect.width() > 0:
                painter.setPen(QColor(230, 230, 230))
                label = filename or ""
                if clip_solo:
                    label += " [S]"
                if track_type == "caption":
                    label = caption_label(painter, label, text_rect)
                painter.drawText(text_rect, Qt.AlignCenter, label)
            painter.setOpacity(1.0)
        painter.restore()


class TrackDisplayDialog(QDialog):
    # Background color (or transparent) and height in pixels for one track
    def __init__(self, parent, bg_color, height_px):
        super().__init__(parent)
        self.setWindowTitle("Track Display Properties")
        self.bg_color = bg_color or ""
        layout = QVBoxLayout(self)

        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Background:"))
        self.color_button = QPushButton()
        self.color_button.clicked.connect(self._pick_color)
        color_row.addWidget(self.color_button)
        layout.addLayout(color_row)

        self.transparent_check = QCheckBox("Transparent")
        self.transparent_check.setChecked(not self.bg_color)
        self.transparent_check.toggled.connect(self._update_button)
        layout.addWidget(self.transparent_check)

        height_row = QHBoxLayout()
        height_row.addWidget(QLabel("Height (px):"))
        self.height_edit = QLineEdit(str(height_px or DEFAULT_TRACK_HEIGHT))
        height_row.addWidget(self.height_edit)
        layout.addLayout(height_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_button()

    def _pick_color(self):
        start = QColor(self.bg_color) if self.bg_color else QColor(60, 60, 60)
        color = QColorDialog.getColor(start, self, "Track Background")
        if color.isValid():
            self.bg_color = color.name()
            self.transparent_check.setChecked(False)
            self._update_button()

    def _update_button(self):
        if self.transparent_check.isChecked() or not self.bg_color:
            self.color_button.setText("Choose...")
            self.color_button.setStyleSheet("")
        else:
            self.color_button.setText(self.bg_color)
            self.color_button.setStyleSheet(f"background-color: {self.bg_color};")

    def _accept(self):
        if self.height_px() is None:
            QMessageBox.warning(self, "Track Display Properties", "Height must be a positive whole number")
            return
        self.accept()

    def height_px(self):
        try:
            value = int(self.height_edit.text().strip())
        except ValueError:
            return None
        return value if value > 0 else None

    def color_value(self):
        if self.transparent_check.isChecked():
            return ""
        return self.bg_color or ""


# Shared registry so Sequence Manager and Project Manager never open duplicates
_open_windows = {}

# Set True by Project Manager before it quits so closeEvents keep open_timelines
app_quitting = False


def set_app_quitting(value=True):
    global app_quitting
    app_quitting = value


def snapshot_open_timelines():
    # Write ids of every visible timeline to open_timelines. Source of truth on quit.
    ids = []
    for sequence_id, win in list(_open_windows.items()):
        if win is not None and win.isVisible() and sequence_id not in ids:
            ids.append(sequence_id)
    _write_open_ids(ids)
    # Players open on those timelines go to open_players
    player_ids = []
    for sequence_id in ids:
        player = getattr(_open_windows.get(sequence_id), "_player", None)
        if player is not None and not player.closed:
            player_ids.append(sequence_id)
    _write_open_player_ids(player_ids)
    return ids


def open_timeline(parent, sequence_id):
    # Raise an existing visible window for this id, or open a new one.
    win = _open_windows.get(sequence_id)
    if win is not None and win.isVisible():
        win.raise_()
        win.activateWindow()
        return win
    win = SequenceTimeline(parent, sequence_id)
    _open_windows[sequence_id] = win
    win.show()
    return win


class SequenceTimeline(QMainWindow):
    # Caption methods live in sequence_timeline_captions.py
    _add_caption_track = _add_caption_track
    _sequence_size = _sequence_size
    add_caption = add_caption
    edit_caption = edit_caption
    # Audio methods live in sequence_timeline_audio.py
    _add_audio_track = _add_audio_track
    _peaks_seconds = _peaks_seconds
    _first_audio_track_id = _first_audio_track_id
    _audio_length_frames = _audio_length_frames
    peaks_for = peaks_for
    # Video methods live in sequence_timeline_video.py
    _add_video_track = _add_video_track
    _generate_out_icon = _generate_out_icon
    _clip_is_video = _clip_is_video
    # Image methods live in sequence_timeline_image.py
    _add_image_track = _add_image_track
    # Keyframe methods live in sequence_timeline_keyframe.py / keyframe.py
    _add_keyframe_track = _add_keyframe_track

    def add_keyframe_clip(self, track_id, click_frame):
        from keyframe import add_keyframe_clip as _add_kf_clip
        _add_kf_clip(self, track_id, click_frame)
    # SQL and data methods live in sequence_timeline_sql.py
    _add_track = _add_track
    _save_track_display = _save_track_display
    _move_track = _move_track
    _toggle_track_flag = _toggle_track_flag
    drop_footage = drop_footage
    _range_overlaps = _range_overlaps
    delete_selected_clip = delete_selected_clip
    slice_clip = slice_clip
    ripple_delete = ripple_delete
    _toggle_clip_flag = _toggle_clip_flag

    def __init__(self, parent=None, sequence_id=None):
        super().__init__(parent)
        name = _load_sequence_name(sequence_id)
        if name:
            self.setWindowTitle(f"Sequence Timeline: {name}: #{sequence_id}")
        else:
            self.setWindowTitle(f"Sequence Timeline: #{sequence_id}")
        self.resize(900, 400)
        self.sequence_id = sequence_id
        self._geometry_ready = False

        self.fps = _load_sequence_fps(sequence_id)
        self.pixels_per_frame = 2
        self.current_frame = 0
        self.view_start_frame = 0
        self.length_seconds = sequence_length_seconds(sequence_id)
        self._view_state_ready = False
        self._restore_view_state()

        self.table = QTableWidget()
        self.ruler = RulerHeader(self)
        self.table.setHorizontalHeader(self.ruler)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Name", "Type", ""])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Only Name items are editable (set per item in refresh)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        self._filling = False
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._track_context_menu)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.verticalHeader().setVisible(False)
        self.ruler.setStretchLastSection(False)
        # Name is a user-resizable column. A true Stretch fights the
        # scrolling ruler (Clips is wider than the view), so it is not used.
        self.ruler.setSectionResizeMode(0, QHeaderView.Interactive)
        self.ruler.resizeSection(0, 200)
        if self.sequence_id is not None:
            self._restore_name_width()
        self.ruler.sectionResized.connect(self._on_section_resized)
        self.ruler.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        # Clips fills the remaining window width; the ruler is virtual
        self.ruler.setSectionResizeMode(CLIPS_COL, QHeaderView.Stretch)
        # Central area: track table on top, play control row pinned bottom left
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.table, 1)
        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(0)
        controls_row.addStretch(0)
        for label, name in (("<<", "Jump to home"),
                            ("|<", "Jump to previous edit"),
                            ("<", "Play in reverse"),
                            ("||", "Pause"),
                            (">", "Play"),
                            (">|", "Jump to next edit"),
                            (">>", "Jump to end")):
            btn = QPushButton(label, central)
            btn.setFixedSize(64, 64)
            btn.setToolTip(name)
            btn.clicked.connect(lambda checked=False, n=name: self._on_play_control(n))
            controls_row.addWidget(btn)
        controls_row.addStretch(1)
        self._controls_row = controls_row
        self._apply_controls_alignment()
        central_layout.addLayout(controls_row)
        self.setCentralWidget(central)
        # Wheel over the track rows also pans
        self.table.viewport().installEventFilter(self)
        # Playhead center line down the tracks, drawn over the clips
        self.playhead_overlay = PlayheadOverlay(self, self.table.viewport())
        # Preview worker: repaint the ruler bar as rows are saved (no reload)
        self._preview_center = None
        self._preview_seen = -1
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(500)
        self._preview_timer.timeout.connect(self._preview_tick)
        self._preview_timer.start()
        # Play: fill the remainder, then an elapsed-time clock off the preview
        self._play_filling = False
        self._playing = False
        self._play_from = 0
        self._play_start_frame = 0
        self._play_start_time = 0.0
        self._play_shown_count = 0
        self._play_last_shown = None
        self._play_audio_clock = False  # True after first video frame is shown
        self._play_prime_tries = 0
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(20)
        self._play_timer.timeout.connect(self._play_tick)
        # Clips drawn by a delegate; footage dropped from Footage Manager
        self.clips_by_track = {}
        self._icon_cache = {}
        self._peaks_cache = {}   # footage_id -> (pps, duration, array) or None
        # Clip selection: ids for painting, ordered list so "first" is the
        # first clip selected. Selection solo (Alt+click) is in memory only;
        # it is not the clips.solo DB column.
        self._selected_clip_ids = set()
        self._selection_order = []
        self._selection_solo = False
        self._out_paint_miss = {}   # clip_id -> (frame, height) last printed miss
        self.table.setItemDelegateForColumn(CLIPS_COL, ClipDelegate(self))
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        # Clip edge dragging: hover cursor needs mouse tracking
        self.table.viewport().setMouseTracking(True)
        self._edge_drag = None
        self._key_drag = None
        self._snap_frame = None   # white snap line frame, None = hidden

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        # Zoom keys work regardless of which child has focus
        for keys in ["+", "=", "Ctrl+="]:
            QShortcut(QKeySequence(keys), self, activated=self.zoom_in)
        QShortcut(QKeySequence("-"), self, activated=self.zoom_out)
        QShortcut(QKeySequence(Qt.Key_Home), self, activated=self.go_home)
        QShortcut(QKeySequence(Qt.Key_End), self, activated=self.go_end)
        QShortcut(QKeySequence(Qt.Key_Delete), self, activated=self.delete_selected_clip)
        # Space = Play / Pause for this timeline. Window shortcut only; text
        # fields and spin boxes take Space first (shortcut override)
        QShortcut(QKeySequence(Qt.Key_Space), self, activated=self._toggle_play)

        self._create_menus()
        self.refresh()
        self._update_clips_width()
        self._update_status()
        self._restore_geometry()
        self._geometry_ready = True
        self._view_state_ready = True

    def _create_menus(self):
        menubar = self.menuBar()

        # Edit menu (in-memory clipboard per sequence, timeline_clipboard.py)
        edit_menu = menubar.addMenu("Edit")
        self._copy_action = QAction("Copy", self)
        self._copy_action.triggered.connect(self._clip_copy)
        self._paste_tic_action = QAction("Paste To Tic", self)
        self._paste_tic_action.triggered.connect(self._clip_paste_tic)
        self._paste_end_action = QAction("Paste to End", self)
        self._paste_end_action.triggered.connect(self._clip_paste_end)
        self._cut_action = QAction("Cut", self)
        self._cut_action.triggered.connect(self._clip_cut)
        for a in (self._copy_action, self._paste_tic_action,
                  self._paste_end_action, self._cut_action):
            edit_menu.addAction(a)
        edit_menu.aboutToShow.connect(self._update_edit_actions)
        self._update_edit_actions()

        # Tracks menu
        tracks_menu = menubar.addMenu("Tracks")
        for label in ["+ Video", "+ Audio", "+ Image", "+ Caption", "+ Keyframe"]:
            action = QAction(label, self)
            if label == "+ Image":
                action.triggered.connect(self._add_image_track)
            elif label == "+ Video":
                action.triggered.connect(self._add_video_track)
            elif label == "+ Audio":
                action.triggered.connect(self._add_audio_track)
            elif label == "+ Caption":
                action.triggered.connect(self._add_caption_track)
            elif label == "+ Keyframe":
                action.triggered.connect(self._add_keyframe_track)
            else:
                action.triggered.connect(lambda checked=False, l=label: self._stub(l))
            tracks_menu.addAction(action)

        # Player: one top-level menu item, no dropdown (stub for now)
        player_action = QAction("Player", self)
        player_action.triggered.connect(lambda checked=False: self.open_player())
        menubar.addAction(player_action)

    def _project_db(self):
        return _get_global_setting("current_project", "")

    def _update_edit_actions(self):
        # Copy/Cut need a selection; Paste needs this sequence's clipboard
        if not hasattr(self, "_copy_action"):
            return
        try:
            from timeline_clipboard import has_clipboard
            has = has_clipboard(self)
        except Exception:
            has = False
        sel = bool(getattr(self, "_selected_clip_ids", None))
        self._copy_action.setEnabled(sel)
        self._cut_action.setEnabled(sel)
        self._paste_tic_action.setEnabled(has)
        self._paste_end_action.setEnabled(has)

    def _clip_copy(self):
        from timeline_clipboard import copy_selection
        n = copy_selection(self)
        if n:
            self.status_bar.showMessage(f"Copied {n} item(s)", 3000)
        self._update_edit_actions()

    def _clip_cut(self):
        from timeline_clipboard import cut_selection
        cut_selection(self)
        self._update_edit_actions()

    def _clip_paste_tic(self):
        from timeline_clipboard import paste
        paste(self, int(self.current_frame), True)
        self._update_edit_actions()

    def _clip_paste_end(self):
        from timeline_clipboard import paste, end_anchor
        paste(self, end_anchor(self), False)
        self._update_edit_actions()


    def _row_track_id(self, row):
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    def _track_context_menu(self, pos):
        # Key boxes first so right-click does not open the clip menu
        from keyframe import hit_test_key, key_context_menu
        key_hit = hit_test_key(self, pos)
        if key_hit is not None:
            key_context_menu(self, key_hit, pos)
            return
        # Markers next: Edit / Delete instead of the clip menu
        from markers import hit_test_marker, marker_context_menu
        marker_hit = hit_test_marker(self, pos)
        if marker_hit is not None:
            marker_context_menu(self, marker_hit, pos)
            return
        # Right-click on an image or video clip gets the clip menu instead
        clip = self._clip_at(pos)
        if clip is not None:
            self._clip_context_menu(clip, pos)
            return
        item = self.table.itemAt(pos)
        if item is None:
            return
        name_item = self.table.item(item.row(), 0)
        if name_item is None:
            return
        track_id = name_item.data(Qt.UserRole)
        if track_id is None:
            return
        enabled = bool(name_item.data(Qt.UserRole + 1))

        menu = QMenu(self)
        up_action = menu.addAction("Move Up")
        down_action = menu.addAction("Move Down")
        enable_action = menu.addAction("Disable" if enabled else "Enable")
        solo_action = menu.addAction("Solo")
        solo_action.setCheckable(True)
        solo_action.setChecked(bool(name_item.data(Qt.UserRole + 2)))
        display_action = menu.addAction("Track Display Properties")
        ripple_action = None
        add_caption_action = None
        add_keyframe_action = None
        x_in_clips = self._x_in_clips(pos)
        track_type = (name_item.data(Qt.UserRole + 4) or "").lower()
        if x_in_clips >= 0 and track_type == "keyframe":
            menu.addSeparator()
            add_keyframe_action = menu.addAction("Add Keyframe Clip")
        elif x_in_clips >= 0 and track_type in EDIT_TRACK_TYPES:
            menu.addSeparator()
            if track_type == "caption":
                add_caption_action = menu.addAction("Add Caption")
            ripple_action = menu.addAction("Ripple Delete")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if add_keyframe_action is not None and action == add_keyframe_action:
            click_frame = self.view_start_frame + x_in_clips / self.pixels_per_frame
            self.add_keyframe_clip(track_id, click_frame)
            return
        if add_caption_action is not None and action == add_caption_action:
            click_frame = self.view_start_frame + x_in_clips / self.pixels_per_frame
            self.add_caption(track_id, click_frame)
            return
        if ripple_action is not None and action == ripple_action:
            click_frame = self.view_start_frame + x_in_clips / self.pixels_per_frame
            self.ripple_delete(track_id, click_frame)
            return
        if action == up_action:
            self._move_track(track_id, -1)
        elif action == down_action:
            self._move_track(track_id, 1)
        elif action == enable_action:
            self._toggle_track_flag(track_id, "enabled")
        elif action == solo_action:
            self._toggle_track_flag(track_id, "solo")
        elif action == display_action:
            self._track_display_properties(track_id)

    def _track_display_properties(self, track_id):
        project_db = self._project_db()
        try:
            conn = sqlite3.connect(project_db)
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(height_px, 64), COALESCE(bg_color, '') FROM tracks WHERE id = ?",
                (track_id,)
            )
            row = cur.fetchone()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Track Display Properties", f"Could not read track: {e}")
            return
        if row is None:
            return
        dlg_bg = row[1] or ""
        if dlg_bg.lower() == "#403050":
            ttype = self._track_type(track_id)
            if ttype == "keyframe":
                dlg_bg = ""
        dialog = TrackDisplayDialog(self, dlg_bg, row[0])
        if dialog.exec() != QDialog.Accepted:
            return
        self._save_track_display(track_id, dialog.height_px(), dialog.color_value())


    def _on_item_changed(self, item):
        # Name edits only; ignore changes made while refresh fills the table
        if self._filling or item.column() != 0:
            return
        track_id = item.data(Qt.UserRole)
        if track_id is None:
            return
        name = item.text().strip()
        if not name:
            # Name is required; put the stored name back
            self.refresh()
            return
        try:
            conn = sqlite3.connect(self._project_db())
            cur = conn.cursor()
            cur.execute("SELECT name FROM tracks WHERE id = ?", (track_id,))
            row = cur.fetchone()
            if row is not None and row[0] == name:
                conn.close()
                return
            cur.execute("UPDATE tracks SET name = ? WHERE id = ?", (name, track_id))
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Rename Track", f"Could not rename track: {e}")
            self.refresh()
            return
        self.status_bar.showMessage(f"Renamed track: {name}", 3000)
        self._refresh_sequence_manager()

    def _after_track_change(self):
        self.refresh()
        self._refresh_sequence_manager()

    def _refresh_sequence_manager(self):
        # Refresh any open Sequence Manager so its counts stay current
        for w in QApplication.allWidgets():
            if type(w).__name__ == "SequenceManager" and w.isVisible():
                try:
                    w.refresh()
                except Exception:
                    pass

    def _footage_id_from_mime(self, mime):
        if mime is None or not mime.hasText():
            return None
        text = mime.text().strip()
        if not text.startswith("footage:"):
            return None
        value = text[len("footage:"):].strip()
        return int(value) if value.isdigit() else None

    def _handle_drop(self, event):
        footage_id = self._footage_id_from_mime(event.mimeData())
        if footage_id is None:
            event.ignore()
            return
        pos = event.position().toPoint()
        row = self.table.rowAt(pos.y())
        x_in_clips = pos.x() - self.ruler.sectionViewportPosition(CLIPS_COL)
        if self.drop_footage(footage_id, row, x_in_clips):
            event.acceptProposedAction()
        else:
            event.ignore()


    def _linked_clips(self, clip):
        # Other clips sharing clip's link_id (any track), as live lists.
        # Footage, caption, and keyframe clips all participate.
        link_id = clip[8] if len(clip) > 8 else None
        if link_id is None:
            return []
        return [c for clips in self.clips_by_track.values() for c in clips
                if c[0] != clip[0] and len(c) > 8 and c[8] == link_id]

    def _track_row_height(self, track_id):
        # Icon height for a track, equal to the delegate's option.rect.height().
        # QTableView trims the grid line (1px) off each cell's rect, so the
        # painted height is rowHeight - 1 when the grid is shown. Using plain
        # rowHeight here made the out icon cache key miss paint's key.
        grid = 1 if self.table.showGrid() else 0
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == track_id:
                return max(0, self.table.rowHeight(r) - grid)
        return 0

    def _read_icon_blob(self, footage_id, frame_number, height):
        # Stored resized icon for footage_id at frame_number that is exactly
        # height tall (real stored size, width follows aspect), as a QPixmap
        conn = sqlite3.connect(self._project_db())
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(icons)")
            if not cur.fetchall():
                return None
            cur.execute(
                "SELECT data FROM icons WHERE footage_id = ? AND frame_number = ? "
                "AND is_original = 0 AND height = ? AND data IS NOT NULL "
                "ORDER BY id DESC",
                (footage_id, frame_number, height)
            )
            for row in cur.fetchall():
                loaded = QPixmap()
                if loaded.loadFromData(bytes(row[0])) and loaded.height() == height:
                    return loaded
            return None
        finally:
            conn.close()

    def icon_pixmap(self, footage_id, height, frame_number=0, generate=True, clip_id=None):
        # Footage icon for frame_number exactly height tall, width follows
        # aspect (6240x4160 at 119 gives 179x119). With generate,
        # ensure_footage_icon looks up that height, else scales the stored full
        # frame, else grabs it. Mid-drag (generate False) only a stored icon of
        # this size is used. Misses are not cached, so a later call tries again.
        frame_number = max(0, int(frame_number or 0))
        key = (footage_id, frame_number, height)
        if key in self._icon_cache:
            return self._icon_cache[key]
        # Printed on cache misses only, so repaints of cached icons stay quiet
        # print(f"ICON: timeline icon_pixmap clip_id={clip_id} footage_id={footage_id} "
              # f"in_frame={frame_number} height={height} generate={generate}")
        if height <= 0:
            return None
        try:
            if generate:
                from footage_manager_display import ensure_footage_icon
                pix = ensure_footage_icon(
                    self._project_db(), footage_id,
                    frame_number=frame_number,
                    target_height=height
                )
            else:
                pix = self._read_icon_blob(footage_id, frame_number, height)
            if pix is None or pix.isNull():
                return None
        except Exception:
            return None
        self._icon_cache[key] = pix
        return pix

    def go_home(self):
        self.view_start_frame = 0
        self.current_frame = 0
        self._clamp_view()
        self.ruler.viewport().update()
        self.table.viewport().update()
        self._update_status()
        self._save_view_state()

    def go_end(self):
        self.view_start_frame = max(0, self.total_frames() - self.visible_frames())
        self._clamp_view()
        self.current_frame = self.total_frames() - 1
        self.ruler.viewport().update()
        self.table.viewport().update()
        self._update_status()
        self._save_view_state()

    def total_frames(self):
        return max(1, int(round(self.length_seconds * self.fps)))

    def visible_frames(self):
        return max(1, int(self.ruler.sectionSize(CLIPS_COL) / self.pixels_per_frame))

    def _clamp_view(self):
        max_start = max(0, self.total_frames() - self.visible_frames())
        self.view_start_frame = max(0, min(max_start, int(self.view_start_frame)))

    def _update_clips_width(self):
        # Clips width is set by the Stretch mode; just keep the view valid and repaint
        self._clamp_view()
        self.ruler.viewport().update()
        self.table.viewport().update()

    def wheel_pan(self, event):
        # Wheel up pans earlier, wheel down pans later. About one second per notch.
        dy = event.angleDelta().y()
        if dy == 0:
            event.ignore()
            return
        if event.modifiers() & Qt.ControlModifier:
            # Ctrl+wheel zooms: up = zoom in, down = zoom out, keeping the
            # frame under the mouse in place
            x_in_clips = event.position().x() - self.ruler.sectionViewportPosition(CLIPS_COL)
            anchor = self.view_start_frame + max(0.0, x_in_clips) / self.pixels_per_frame
            if dy > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            if x_in_clips >= 0:
                self.view_start_frame = max(0, int(round(
                    anchor - x_in_clips / self.pixels_per_frame)))
                self._clamp_view()
                self.ruler.viewport().update()
                self.table.viewport().update()
                self._update_status()
                self._save_view_state()
            event.accept()
            return
        step = int(self.fps) or 30
        notches = max(1, abs(dy) // 120)
        if dy > 0:
            self.view_start_frame -= step * notches
        else:
            self.view_start_frame += step * notches
        self._clamp_view()
        self.ruler.viewport().update()
        self.table.viewport().update()
        self._update_status()
        self._save_view_state()
        event.accept()

    def eventFilter(self, obj, event):
        if obj is self.table.viewport():
            etype = event.type()
            if etype == QEvent.Resize and getattr(self, "playhead_overlay", None):
                self.playhead_overlay.setGeometry(obj.rect())
                self.playhead_overlay.raise_()
            if etype == QEvent.Wheel:
                self.wheel_pan(event)
                return True
            if etype in (QEvent.DragEnter, QEvent.DragMove):
                if self._footage_id_from_mime(event.mimeData()) is not None:
                    event.acceptProposedAction()
                else:
                    event.ignore()
                return True
            if etype == QEvent.Drop:
                self._handle_drop(event)
                return True
            if etype == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                # Double-click a key box: edit value only (no clip actions)
                from keyframe import hit_test_key, edit_key
                dbl_pos = event.position().toPoint()
                key_hit = hit_test_key(self, dbl_pos)
                if key_hit is not None:
                    edit_key(self, key_hit["clip"], key_hit["key_id"],
                             key_hit["frame"], key_hit["value"])
                    return True
                return False
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                # Click selection: normal = clip + link partners, Alt = only
                # that clip (selection solo), Shift = add to the selection,
                # empty clip-area space = clear
                press_pos = event.position().toPoint()
                # Marker under the mouse: marker drag only (no clip select /
                # drag, no edit popup)
                from markers import hit_test_marker, marker_drag_start
                marker_hit = hit_test_marker(self, press_pos)
                if marker_hit is not None:
                    marker_drag_start(self, marker_hit, press_pos)
                    return True
                hit_clip = self._clip_at(press_pos)
                edge_hit = self._clip_edge_at(press_pos)
                if hit_clip is None and edge_hit is not None:
                    hit_clip = edge_hit[0]
                # Event modifiers plus the live keyboard state, so Alt is seen
                # even if the platform drops it from the mouse event
                mods = event.modifiers() | QApplication.queryKeyboardModifiers()
                if hit_clip is None:
                    if self._x_in_clips(press_pos) >= 0:
                        self._clear_selection()
                elif mods & Qt.AltModifier:
                    # Alt: ONLY this clip, no link partners, solo on
                    self._select_clip(hit_clip, "solo")
                elif mods & Qt.ShiftModifier:
                    self._select_clip(hit_clip, "add")
                elif hit_clip[0] in self._selected_clip_ids:
                    # Already selected (solo or multi): keep it so an edge
                    # or move drag uses the same selection and does not clear
                    # Alt solo
                    pass
                else:
                    self._select_clip(hit_clip, "normal")
                from keyframe import hit_test_key
                key_hit = hit_test_key(self, press_pos)
                if key_hit is not None:
                    self._key_drag = {
                        "clip": key_hit["clip"],
                        "index": key_hit["index"],
                        "key_id": key_hit["key_id"],
                        "orig_frame": key_hit["frame"],
                        "frame": key_hit["frame"],
                        "value": key_hit["value"],
                        "press_pos": press_pos,
                        "dragging": False,
                    }
                    return True
                hit = self._clip_edge_at(event.position().toPoint())
                if hit is not None:
                    clip, edge = hit
                    self._edge_drag = {
                        "clip": clip,
                        "edge": edge,
                        "orig_start": clip[2],
                        "orig_length": clip[3],
                        "orig_in": clip[7],
                        "snap_px": self._snap_pixels(),
                    }
                    self._edge_drag["group"] = self._drag_group(clip, edge)
                    return True
                clip = self._clip_at(event.position().toPoint())
                if clip is not None:
                    press_frame = self._frame_at_clips_x(self._x_in_clips(event.position().toPoint()))
                    self._edge_drag = {
                        "clip": clip,
                        "edge": "move",
                        "orig_start": clip[2],
                        "orig_length": clip[3],
                        "grab_offset": press_frame - clip[2],
                        "src_track": self._clip_track_id(clip),
                        "target_track": None,
                        "target_ok": True,
                        "snap_px": self._snap_pixels(),
                    }
                    self._edge_drag["group"] = self._drag_group(clip, "move")
                    return True
            if etype == QEvent.MouseMove:
                pos = event.position().toPoint()
                if getattr(self, "_marker_drag", None) is not None:
                    from markers import marker_drag_move
                    marker_drag_move(self, pos)
                    return True
                if self._key_drag is not None:
                    from keyframe import key_drag_move
                    key_drag_move(self, pos)
                    return True
                if self._edge_drag is not None:
                    self._edge_drag_move(pos)
                    return True
                if self._clip_edge_at(pos) is not None:
                    obj.setCursor(Qt.SplitHCursor)
                else:
                    obj.unsetCursor()
                # Hovered marker: 3 px line; repaint only when it changes
                from markers import hit_test_marker
                mh = hit_test_marker(self, pos)
                self._set_hover_marker(mh[1][0] if mh is not None else None)
            if etype == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if getattr(self, "_marker_drag", None) is not None:
                    from markers import marker_drag_release
                    marker_drag_release(self)
                    return True
                if self._key_drag is not None:
                    from keyframe import key_drag_release
                    key_drag_release(self)
                    return True
                if self._edge_drag is not None:
                    if self._edge_drag["edge"] == "move":
                        self._move_drag_release()
                    else:
                        self._edge_drag_release()
                    return True
            if etype == QEvent.Leave:
                self._set_hover_marker(None)
            if etype == QEvent.Leave and self._edge_drag is None:
                obj.unsetCursor()
        return super().eventFilter(obj, event)

    def _set_hover_marker(self, marker_id):
        if getattr(self, "_hover_marker_id", None) == marker_id:
            return
        self._hover_marker_id = marker_id
        self.table.viewport().update()

    def _x_in_clips(self, pos):
        return pos.x() - self.ruler.sectionViewportPosition(CLIPS_COL)

    def _frame_at_clips_x(self, x_in_clips):
        # Same conversion as drop
        return max(0, self.view_start_frame + int(x_in_clips / self.pixels_per_frame))

    def _clip_edge_at(self, pos):
        # Return (clip, "left" or "right") if pos is within EDGE_HIT_PX of an
        # image or video clip edge, else None
        row = self.table.rowAt(pos.y())
        if row < 0:
            return None
        name_item = self.table.item(row, 0)
        if name_item is None:
            return None
        if (name_item.data(Qt.UserRole + 4) or "").lower() not in EDIT_TRACK_TYPES:
            return None
        x_in_clips = self._x_in_clips(pos)
        if x_in_clips < -EDGE_HIT_PX:
            return None
        ppf = self.pixels_per_frame
        best = None
        best_dist = EDGE_HIT_PX + 1
        for clip in self.clips_by_track.get(name_item.data(Qt.UserRole), []):
            left = int((clip[2] - self.view_start_frame) * ppf)
            width = max(1, int(clip[3] * ppf))
            # Last painted pixel column of the clip (same rect the delegate draws)
            right = left + width - 1
            for edge, edge_x in (("left", left), ("right", right)):
                dist = abs(x_in_clips - edge_x)
                # <= so the right edge wins a tie on very narrow clips
                if dist <= EDGE_HIT_PX and (dist < best_dist or
                                            (edge == "right" and dist == best_dist)):
                    best = (clip, edge)
                    best_dist = dist
        return best

    def _clip_by_id(self, clip_id):
        for clips in self.clips_by_track.values():
            for c in clips:
                if c[0] == clip_id:
                    return c
        return None

    def _clear_selection(self):
        if self._selected_clip_ids or self._selection_solo:
            self._selected_clip_ids = set()
            self._selection_order = []
            self._selection_solo = False
            self.table.viewport().update()
        self._update_edit_actions()

    def _select_clip(self, clip, mode):
        # mode: "normal" = clip + link partners, "solo" = only clip with
        # selection solo on, "add" = add clip and its link partners (keep
        # existing selection; drop nothing)
        if mode == "add":
            if self._selection_solo:
                self._selection_solo = False
            members = [clip] + self._linked_clips(clip)
            for m in members:
                if m[0] not in self._selected_clip_ids:
                    self._selected_clip_ids.add(m[0])
                    self._selection_order.append(m[0])
        elif mode == "solo":
            self._selected_clip_ids = {clip[0]}
            self._selection_order = [clip[0]]
            self._selection_solo = True
        else:
            members = [clip] + self._linked_clips(clip)
            self._selection_order = [c[0] for c in members]
            self._selected_clip_ids = set(self._selection_order)
            self._selection_solo = False
        self.table.viewport().update()
        self._update_edit_actions()

    def _selected_clips(self):
        # Live clip lists of the selection, in selection order
        out = []
        for cid in self._selection_order:
            c = self._clip_by_id(cid)
            if c is not None:
                out.append(c)
        return out

    def _drag_group(self, clip, edge="move"):
        # Clips that take the drag delta, each with its drag-start values
        # and its own-track neighbor bounds (group members ignored).
        # Solo: only clip. Move of a selected clip: every selected clip plus
        # their link partners. Otherwise: clip plus its link partners.
        if self._selection_solo:
            members = [clip]
        elif edge == "move" and clip[0] in self._selected_clip_ids:
            members = [clip]
            for c in self._selected_clips() + [clip]:
                for m in [c] + self._linked_clips(c):
                    if all(m[0] != x[0] for x in members):
                        members.append(m)
        else:
            members = [clip] + self._linked_clips(clip)
        ids = {c[0] for c in members}
        group = []
        from keyframe import is_keyframe_clip_id
        for c in members:
            prev_end, next_start = self._neighbor_bounds(c, c[2], c[3], ignore_ids=ids)
            entry = {"clip": c, "start": c[2], "length": c[3], "in": c[7],
                     "prev_end": prev_end, "next_start": next_start}
            if is_keyframe_clip_id(c[0]) and len(c) > 9:
                entry["keys"] = list(c[9])
            group.append(entry)
        return group

    def _edge_drag_move(self, pos):
        # The dragged clip asks for a frame delta; every clip in its link
        # group takes the same delta, clamped so no member leaves its own
        # neighbors' gap (each on its own track)
        drag = self._edge_drag
        clip = drag["clip"]
        group = drag.get("group") or self._drag_group(clip)
        drag["group"] = group
        frame = self._frame_at_clips_x(self._x_in_clips(pos))
        edge = drag["edge"]
        if edge == "move":
            target = self._move_target_track(pos, drag)
            if target is not None:
                # Track move only: start_frame stays at drag-start; mouse x
                # does not change time. Release, then drag again, for time.
                self._cross_track_preview(drag, target)
                return
            if drag.get("target_track") is not None:
                # Back on the source track: return the clip to its own row
                # and restore the drag-start start_frame before time move
                self._relocate_clip(clip, drag["src_track"])
                clip[2] = drag["orig_start"]
                drag["target_track"] = None
                drag["target_ok"] = True
            delta = (frame - drag["grab_offset"]) - drag["orig_start"]
        elif edge == "left":
            delta = frame - drag["orig_start"]
        else:
            delta = frame - (drag["orig_start"] + drag["orig_length"])
        delta = self._snap_drag_delta(drag, group, pos, delta)
        lo, hi = self._group_delta_range(group, edge)
        delta = max(lo, min(hi, delta)) if lo <= hi else 0
        for m in group:
            c = m["clip"]
            if edge == "move":
                c[2] = m["start"] + delta
                if "keys" in m:
                    c[9] = [(item[0], item[1] + delta, item[2])
                            for item in m["keys"]]
            elif edge == "left":
                c[2] = m["start"] + delta
                c[3] = m["length"] - delta
                c[7] = m["in"] + delta
                if "keys" in m:
                    c[9] = [(item[0], item[1] + delta, item[2])
                            for item in m["keys"]]
            else:
                c[3] = m["length"] + delta
        self.table.viewport().update()
        self.status_bar.showMessage(f"Clip start {clip[2]}, length {clip[3]}")

    def _snap_pixels(self):
        # Global setting 'Pixel Snap-To' (pixels, default 2), read per drag
        try:
            return max(0, int(_get_global_setting("Pixel Snap-To", "2") or "2"))
        except (TypeError, ValueError):
            return 2

    def _snap_drag_delta(self, drag, group, pos, delta):
        # Move: the dragged clip's in or out point; trim: the dragged edge.
        # Snaps to the nearest in/out point of any clip on any track that is
        # not being moved or trimmed (linked partners left in place count),
        # within snap_px pixels. Distance uses the unrounded mouse position,
        # so it is a real pixel distance at the current zoom. The group
        # moves by the one returned delta; the clamp after it still allows
        # butting up to a neighbor.
        snap_px = drag.get("snap_px", 0)
        ppf = float(self.pixels_per_frame or 0)
        if snap_px <= 0 or ppf <= 0:
            self._set_snap_frame(None)
            return delta
        try:
            from clip_snap import snap_delta_target, snap_targets
            x = self._x_in_clips(pos)
            frame_f = max(0.0, self.view_start_frame + x / ppf)
            start, length = drag["orig_start"], drag["orig_length"]
            edge = drag["edge"]
            if edge == "move":
                raw = (frame_f - drag["grab_offset"]) - start
                edges = [start, start + length]
            elif edge == "left":
                raw = frame_f - start
                edges = [start]
            else:
                raw = frame_f - (start + length)
                edges = [start + length]
            ids = {m["clip"][0] for m in group}
            ids.add(drag["clip"][0])
            from markers import marker_frames
            targets = sorted(set(snap_targets(self.clips_by_track, ids)) | set(
                marker_frames(getattr(self, "_markers", None), self.clips_by_track, ids)))
            snapped, target = snap_delta_target(
                raw, edges, targets, ppf, snap_px)
        except Exception as e:
            # print(f"Snap failed: {e}")
            self._set_snap_frame(None)
            return delta
        self._set_snap_frame(target)
        return delta if snapped is None else snapped

    def _set_snap_frame(self, frame):
        # White snap line frame (None = hidden); repaint ruler and tracks
        # only when it changes
        if getattr(self, "_snap_frame", None) == frame:
            return
        self._snap_frame = frame
        try:
            self.ruler.viewport().update()
            self.playhead_overlay.update()
        except Exception:
            pass

    def _group_delta_range(self, group, edge):
        # Allowed (lo, hi) frame delta for all members at once
        lo, hi = -10 ** 9, 10 ** 9
        for m in group:
            start, length, in_f = m["start"], m["length"], m["in"]
            prev_end, next_start = m["prev_end"], m["next_start"]
            if edge == "move":
                # start + d >= max(0, prev_end); end + d <= next_start
                lo = max(lo, max(0, prev_end) - start)
                if next_start is not None:
                    hi = min(hi, next_start - (start + length))
            elif edge == "left":
                # start + d >= prev_end and >= 0, in + d >= 0, length - d >= 1
                lo = max(lo, max(0, prev_end) - start, -in_f)
                hi = min(hi, length - 1)
            else:
                # length + d >= 1; end + d <= next_start
                lo = max(lo, 1 - length)
                if next_start is not None:
                    hi = min(hi, next_start - (start + length))
        return lo, hi

    def _neighbor_bounds(self, clip, start, length, ignore_ids=None):
        # (prev_end, next_start) for clip's track: end of the nearest clip
        # before start (0 if none) and start of the nearest clip at or after
        # start + length (None if none). The clip itself is ignored.
        prev_end = 0
        next_start = None
        end = start + length
        for c in self.clips_by_track.get(self._clip_track_id(clip), []):
            if c[0] == clip[0] or (ignore_ids and c[0] in ignore_ids):
                continue
            c_start, c_end = c[2], c[2] + c[3]
            if c_start < start:
                prev_end = max(prev_end, c_end)
            elif c_start >= end or c_start >= start:
                next_start = c_start if next_start is None else min(next_start, c_start)
        return prev_end, next_start

    def _clip_image_track_row(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return None
        name_item = self.table.item(row, 0)
        if name_item is None:
            return None
        if (name_item.data(Qt.UserRole + 4) or "").lower() not in EDIT_TRACK_TYPES:
            return None
        return name_item

    def _clip_at(self, pos):
        # Image or video clip whose body (not an edge) is under pos, else None
        name_item = self._clip_image_track_row(pos)
        if name_item is None:
            return None
        x_in_clips = self._x_in_clips(pos)
        if x_in_clips < 0 or self._clip_edge_at(pos) is not None:
            return None
        ppf = self.pixels_per_frame
        for clip in reversed(self.clips_by_track.get(name_item.data(Qt.UserRole), [])):
            left = (clip[2] - self.view_start_frame) * ppf
            right = left + clip[3] * ppf
            if left <= x_in_clips <= right:
                return clip
        return None

    def _track_type(self, track_id):
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == track_id:
                return (item.data(Qt.UserRole + 4) or "").lower()
        return ""

    def _move_target_track(self, pos, drag):
        # Track under the mouse if it differs from the source track and has
        # the same type (video->video, audio->audio, image->image), else None
        src = drag.get("src_track")
        if src is None:
            return None
        row = self.table.rowAt(pos.y())
        if row < 0:
            return None
        name_item = self.table.item(row, 0)
        if name_item is None:
            return None
        tid = name_item.data(Qt.UserRole)
        if tid is None or tid == src:
            return None
        if (name_item.data(Qt.UserRole + 4) or "").lower() != self._track_type(src):
            return None
        return tid

    def _relocate_clip(self, clip, track_id):
        # Move the in-memory clip list to another track's row (preview only)
        for clips in self.clips_by_track.values():
            for i, c in enumerate(clips):
                if c is clip:
                    del clips[i]
                    break
        self.clips_by_track.setdefault(track_id, []).append(clip)

    def _cross_track_preview(self, drag, target):
        # Track move only: the clicked clip changes row, start_frame stays
        # at drag["orig_start"]. Partners stay put. Mouse x is ignored.
        self._set_snap_frame(None)
        clip = drag["clip"]
        for m in drag.get("group") or []:
            c = m["clip"]
            if c is not clip:
                c[2] = m["start"]
        self._relocate_clip(clip, target)
        clip[2] = drag["orig_start"]
        end = clip[2] + clip[3]
        ok = True
        for c in self.clips_by_track.get(target, []):
            if c is clip:
                continue
            if c[2] < end and c[2] + c[3] > clip[2]:
                ok = False
                break
        drag["target_track"] = target
        drag["target_ok"] = ok
        self.table.viewport().update()
        if ok:
            self.status_bar.showMessage(
                f"Move to track {target}, frame {clip[2]} (time locked)")
        else:
            self.status_bar.showMessage("Overlaps a clip on the target track")

    def _move_drag_release(self):
        drag = self._edge_drag
        self._edge_drag = None
        self._set_snap_frame(None)
        clip = drag["clip"]
        target = drag.get("target_track")
        if target is not None:
            # Track change only: start_frame stays at drag start; partner stays
            new_start = drag["orig_start"]
            refused = not drag.get("target_ok", True)
            if not refused:
                try:
                    conn = sqlite3.connect(self._project_db())
                    cur = conn.cursor()
                    if self._range_overlaps(cur, target, new_start, int(clip[3])):
                        refused = True
                    else:
                        from undo import push_undo
                        push_undo(self._project_db(), "Move clip to track")
                        table, row_id = clip_table_and_id(clip[0])
                        cur.execute(
                            f"UPDATE {table} SET track_id = ? WHERE id = ?",
                            (target, row_id))
                        conn.commit()
                    conn.close()
                except Exception as e:
                    QMessageBox.warning(self, "Move Clip", f"Could not save clip: {e}")
            if refused:
                self._after_clip_change("Move refused: overlaps a clip on the target track")
            else:
                # Same frames, new layer order: that range is dirty
                src_kind = self._track_type(drag.get("src_track"))
                if src_kind in ("video", "image"):
                    s0, ln = int(drag["orig_start"]), int(clip[3])
                    self._mark_preview_dirty([(s0, s0 + ln - 1)])
                elif src_kind == "keyframe":
                    # Above/below changes which linked clips it drives
                    self._mark_preview_dirty(self._move_preview_ranges(
                        [{"clip": clip, "start": drag["orig_start"]}], drag))
                self._after_clip_change(f"Clip moved to track {target}")
            return
        group = drag.get("group") or [{"clip": clip}]
        new_start = max(0, int(clip[2]))
        if new_start == drag["orig_start"]:
            return
        from undo import push_undo
        push_undo(self._project_db(), "Move clip")
        try:
            conn = sqlite3.connect(self._project_db())
            cur = conn.cursor()
            for m in group:
                c = m["clip"]
                from keyframe import is_keyframe_clip_id, persist_keyframe_clip_time
                if is_keyframe_clip_id(c[0]):
                    persist_keyframe_clip_time(cur, c)
                    continue
                table, row_id = clip_table_and_id(c[0])
                cur.execute(f"UPDATE {table} SET start_frame = ? WHERE id = ?",
                            (max(0, int(c[2])), row_id))
            conn.commit()
            conn.close()
            # Preview: a whole linked group alone over its frames moves its
            # clean frames with it (run right after the next forced
            # retarget, see _retarget_preview); otherwise old and new
            # ranges go dirty
            ranges = self._move_preview_ranges(group, drag)
            plan = self._preview_shift_plan(group, drag)
            if plan is None:
                self._mark_preview_dirty(ranges)
            else:
                self._preview_pending_shift = (plan, ranges)
        except Exception as e:
            QMessageBox.warning(self, "Move Clip", f"Could not save clip: {e}")
        self._after_clip_change(f"Clip moved to frame {new_start}")
        leftover = getattr(self, "_preview_pending_shift", None)
        if leftover:
            # The retarget did not run the shift: rebuild instead
            self._preview_pending_shift = None
            self._mark_preview_dirty(leftover[1])

    def _move_preview_ranges(self, group, drag):
        # Inclusive frame ranges a time move changes in the preview: old and
        # new ranges of moved video/image and keyframe clips, plus linked
        # partners left in place (solo drag) that composite with them.
        from keyframe import is_keyframe_clip_id
        ranges = []
        ids = set()
        kf_links = set()
        vis_links = set()
        for m in group:
            c = m["clip"]
            ids.add(c[0])
            link_id = c[8] if len(c) > 8 else None
            if is_keyframe_clip_id(c[0]):
                if link_id is not None:
                    kf_links.add(link_id)
            elif self._track_type(self._clip_track_id(c)) in ("video", "image"):
                if link_id is not None:
                    vis_links.add(link_id)
            else:
                continue
            ln = max(1, int(c[3]))
            old_ln = max(1, int(m.get("length", drag.get("orig_length", c[3]))))
            old = max(0, int(m.get("start", drag["orig_start"])))
            new = max(0, int(c[2]))
            ranges.append((old, old + old_ln - 1))
            ranges.append((new, new + ln - 1))
        if kf_links or vis_links:
            for track_id, clips in self.clips_by_track.items():
                kind = self._track_type(track_id)
                for c in clips:
                    link_id = c[8] if len(c) > 8 else None
                    if c[0] in ids or link_id is None:
                        continue
                    if ((link_id in kf_links and kind in ("video", "image"))
                            or (link_id in vis_links and is_keyframe_clip_id(c[0]))):
                        s0 = max(0, int(c[2]))
                        ranges.append((s0, s0 + max(1, int(c[3])) - 1))
        return ranges

    def _preview_shift_plan(self, group, drag):
        # (first, last, delta) when the clean preview rows can move with the
        # group (FramesShift): the whole linked group moved by one delta and
        # no other video/image clip touches the old or new frame span. Any
        # doubt returns None and the caller marks old and new ranges dirty.
        if self.sequence_id is None or not group:
            return None
        try:
            from keyframe import is_keyframe_clip_id
            ids = set()
            links = set()
            deltas = set()
            span = []
            visual = False
            for m in group:
                c = m["clip"]
                ids.add(c[0])
                if len(c) > 8 and c[8] is not None:
                    links.add(c[8])
                old = int(m.get("start", drag["orig_start"]))
                deltas.add(max(0, int(c[2])) - old)
                kind = self._track_type(self._clip_track_id(c))
                if is_keyframe_clip_id(c[0]) or kind in ("video", "image"):
                    visual = visual or kind in ("video", "image")
                    span.append((old, old + max(1, int(c[3])) - 1))
            if len(deltas) != 1 or not links or not visual or not span:
                return None
            delta = deltas.pop()
            if delta == 0:
                return None
            first = min(a for a, _b in span)
            last = max(b for _a, b in span)
            if first + delta < 0:
                return None
            for track_id, clips in self.clips_by_track.items():
                kind = self._track_type(track_id)
                for c in clips:
                    if c[0] in ids:
                        continue
                    # Part of a moved link left behind: not the whole group
                    if len(c) > 8 and c[8] is not None and c[8] in links:
                        return None
                    if kind not in ("video", "image"):
                        continue
                    s0 = int(c[2])
                    e0 = s0 + max(1, int(c[3])) - 1
                    for a, b in ((first, last), (first + delta, last + delta)):
                        if s0 <= b and e0 >= a:
                            return None
            # print(f"Preview shift plan frames {first}..{last} by {delta}")
            return (first, last, delta)
        except Exception as e:
            # print(f"Preview shift skipped: {e}")
            return None

    def _ranges_for_clips(self, clips):
        # Inclusive (first, last) frame ranges for preview dirty marks.
        ranges = []
        for c in clips:
            in_f = max(0, int(c[2]))
            length = max(1, int(c[3]))
            ranges.append((in_f, in_f + length - 1))
        return ranges

    def _mark_preview_dirty(self, ranges):
        # Clean set drops these frames now (bar turns red); the database
        # UPDATE runs on the preview worker thread. refresh() wakes the worker.
        if self.sequence_id is None or not ranges:
            return
        try:
            from sequence_preview import FramesMarkDirty
            FramesMarkDirty(self._project_db(), self.sequence_id, ranges)
            # Marked again after the next forced retarget: a frame the worker
            # builds from the old snapshot in between stays dirty
            self._preview_remark = (getattr(self, "_preview_remark", None) or []) + list(ranges)
            from sequence_preview_worker import get_worker
            get_worker()
        except Exception as e:
            print(f"Preview dirty mark failed: {e}")

    def _clip_context_menu(self, clip, pos):
        # Right-click on an unselected clip selects it like a normal click
        if clip[0] not in self._selected_clip_ids:
            self._select_clip(clip, "normal")
        from keyframe import is_keyframe_clip_id, add_key as _add_key
        if is_keyframe_clip_id(clip[0]):
            menu = QMenu(self)
            key_menu = menu.addMenu("Key")
            key_click_action = key_menu.addAction("At Mouse Click")
            key_tick_action = key_menu.addAction("At Timeline Tick")
            rename_action = menu.addAction("Rename")
            # Linked: Unlink only; not linked: Link only
            linked = len(clip) > 8 and clip[8] is not None
            link_action = unlink_action = None
            if linked:
                unlink_action = menu.addAction("Unlink")
            else:
                link_action = menu.addAction("Link")
                link_action.setEnabled(len(self._selected_clips()) >= 2)
            menu.addSeparator()
            delete_action = menu.addAction("Delete")
            action = menu.exec(self.table.viewport().mapToGlobal(pos))
            if action is None:
                return
            if action == key_click_action:
                click_frame = self.view_start_frame + self._x_in_clips(pos) / self.pixels_per_frame
                _add_key(self, clip, click_frame)
            elif action == key_tick_action:
                # Playhead frame; add_key clamps it to the clip [in, out)
                _add_key(self, clip, int(self.current_frame))
            elif action == rename_action:
                from keyframe import rename_keyframe_clip
                rename_keyframe_clip(self, clip)
            elif link_action is not None and action == link_action:
                self.link_selected_clips()
            elif unlink_action is not None and action == unlink_action:
                self.unlink_clips(clip)
            elif action == delete_action:
                self.delete_selected_clip()
            return
        if is_caption_id(clip[0]):
            # Caption clip menu: Edit, Slice, Link, Unlink, Delete
            menu = QMenu(self)
            edit_action = menu.addAction("Edit")
            slice_action = menu.addAction("Slice")
            link_action = menu.addAction("Link")
            link_action.setEnabled(len(self._selected_clips()) >= 2)
            unlink_action = menu.addAction("Unlink")
            menu.addSeparator()
            delete_action = menu.addAction("Delete")
            action = menu.exec(self.table.viewport().mapToGlobal(pos))
            if action == edit_action:
                self.edit_caption(clip)
            elif action == slice_action:
                self.slice_clip(clip, self._x_in_clips(pos))
            elif action == link_action:
                self.link_selected_clips()
            elif action == unlink_action:
                self.unlink_clips(clip)
            elif action == delete_action:
                self.delete_selected_clip()
            return
        enabled = bool(clip[5])
        menu = QMenu(self)
        # Gain... on audio-track clips only (the only rows the preview audio
        # mixes); right-clicked clip only, like Enable / Solo
        gain_action = None
        if self._track_type(self._clip_track_id(clip)) == "audio":
            gain_action = menu.addAction("Gain...")
            menu.addSeparator()
        slice_action = menu.addAction("Slice")
        marker_menu = menu.addMenu("Marker")
        marker_click_action = marker_menu.addAction("Add To Mouse Click")
        marker_tick_action = marker_menu.addAction("Add to Time Tick")
        # Linked: Unlink only; not linked: Link only
        clip_linked = len(clip) > 8 and clip[8] is not None
        link_action = unlink_action = None
        if clip_linked:
            unlink_action = menu.addAction("Unlink")
        else:
            link_action = menu.addAction("Link")
            link_action.setEnabled(len(self._selected_clips()) >= 2)
        menu.addSeparator()
        enable_action = menu.addAction("Disable" if enabled else "Enable")
        solo_action = menu.addAction("Solo")
        solo_action.setCheckable(True)
        solo_action.setChecked(bool(clip[6]))
        # Proxy submenu on video / image clips only (not audio)
        proxy_actions = {}
        if self._track_type(self._clip_track_id(clip)) in ("video", "image"):
            from clip_proxy import add_clip_proxy_submenu
            proxy_actions = add_clip_proxy_submenu(self, menu, clip)
        menu.addSeparator()
        delete_action = menu.addAction("Delete")
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action is not None and action in proxy_actions:
            from clip_proxy import apply_clip_proxy
            apply_clip_proxy(self, clip, proxy_actions[action])
            return
        if action == delete_action:
            # Same as the Delete key: selected clips, plus link partners
            # unless selection solo
            self.delete_selected_clip()
            return
        if gain_action is not None and action == gain_action:
            from clip_gain import edit_clip_gain
            edit_clip_gain(self, clip)
            return
        if action is None:
            return
        if action == marker_click_action:
            self._add_marker(clip, pos)
        elif action == marker_tick_action:
            self._add_marker(clip, None)
        elif action == slice_action:
            self.slice_clip(clip, self._x_in_clips(pos))
        elif link_action is not None and action == link_action:
            self.link_selected_clips()
        elif unlink_action is not None and action == unlink_action:
            self.unlink_clips(clip)
        elif action == enable_action:
            self._toggle_clip_flag(clip[0], "enabled")
        elif action == solo_action:
            self._toggle_clip_flag(clip[0], "solo")

    def _add_marker(self, clip, pos):
        # Marker at the frame under the mouse, dark red, blank name (undoable)
        project_db = self._project_db()
        if not project_db:
            return
        if pos is None:
            # Playhead, clamped to the clip's visible range
            from markers import clamp_to_visible
            frame = clamp_to_visible(clip, int(self.current_frame))
        else:
            frame = int(self.view_start_frame + self._x_in_clips(pos) / self.pixels_per_frame)
        from undo import push_undo
        push_undo(project_db, "Add marker")
        from markers import add_marker
        try:
            new_id = add_marker(project_db, clip, frame)
        except Exception as e:
            QMessageBox.warning(self, "Add Marker", f"Could not add marker: {e}")
            return
        if new_id is None:
            self.status_bar.showMessage(
                "Markers are not available in this project (older version)", 3000)
            return
        self._after_clip_change(f"Marker at frame {frame}")

    def link_selected_clips(self):
        # Link: every selected clip gets link_id = first selected clip's id
        # (footage clips / captions / keyframe_clips each store their own)
        selected = list(self._selected_clips())
        if len(selected) < 2:
            return
        new_link = selected[0][0]
        from undo import push_undo
        push_undo(self._project_db(), "Link clips")
        try:
            from keyframe import is_keyframe_clip_id, persist_keyframe_link_id
            from sequence_timeline_captions import persist_caption_link_id
            conn = sqlite3.connect(self._project_db())
            cur = conn.cursor()
            for c in selected:
                if is_keyframe_clip_id(c[0]):
                    persist_keyframe_link_id(cur, c[0], new_link)
                elif is_caption_id(c[0]):
                    persist_caption_link_id(cur, c[0], new_link)
                else:
                    cur.execute("UPDATE clips SET link_id = ? WHERE id = ?",
                                (new_link, c[0]))
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Link Clips", f"Could not link clips: {e}")
            return
        # Scale keyframe link changes affect composite over those ranges
        self._mark_preview_dirty(self._ranges_for_clips(selected))
        self._after_clip_change(f"Linked {len(selected)} clips (link {new_link})")

    def unlink_clips(self, clip):
        # Unlink: the right-clicked clip, or every selected clip when several
        # are selected and the right-clicked clip is one of them
        selected = self._selected_clips()
        if len(selected) > 1 and clip[0] in self._selected_clip_ids:
            ids = [c[0] for c in selected]
        else:
            ids = [clip[0]]
        from undo import push_undo
        push_undo(self._project_db(), "Unlink clips")
        try:
            from keyframe import is_keyframe_clip_id, persist_keyframe_link_id
            from sequence_timeline_captions import persist_caption_link_id
            conn = sqlite3.connect(self._project_db())
            cur = conn.cursor()
            for cid in ids:
                if is_keyframe_clip_id(cid):
                    persist_keyframe_link_id(cur, cid, None)
                elif is_caption_id(cid):
                    persist_caption_link_id(cur, cid, None)
                else:
                    cur.execute("UPDATE clips SET link_id = NULL WHERE id = ?",
                                (cid,))
            conn.commit()
            conn.close()
        except Exception as e:
            QMessageBox.warning(self, "Unlink Clips", f"Could not unlink clips: {e}")
            return
        affected = [c for c in self._selected_clips() if c[0] in ids]
        if not affected:
            affected = [clip]
        self._mark_preview_dirty(self._ranges_for_clips(affected))
        self._after_clip_change(f"Unlinked {len(ids)} clip(s)")


    def _after_clip_change(self, message):
        self.refresh()
        self._update_edit_actions()
        self.table.viewport().update()
        self.ruler.viewport().update()
        self._refresh_sequence_manager()
        self._clamp_view()
        self.current_frame = max(0, min(self.total_frames() - 1, self.current_frame))
        self._save_view_state()
        self.status_bar.showMessage(message, 3000)


    def _edge_drag_release(self):
        drag = self._edge_drag
        self._edge_drag = None
        self._set_snap_frame(None)
        clip = drag["clip"]
        # print(f"ICON: edge-drag release clip_id={clip[0]} in_frame={clip[7]} "
              # f"edge={drag['edge']} (icon requested at row height on next paint, generate=True)")
        if (clip[2] == drag["orig_start"] and clip[3] == drag["orig_length"]
                and clip[7] == drag["orig_in"]):
            return
        group = drag.get("group") or [{"clip": clip}]
        from undo import push_undo
        push_undo(self._project_db(), "Trim clip")
        try:
            conn = sqlite3.connect(self._project_db())
            cur = conn.cursor()
            for m in group:
                c = m["clip"]
                from keyframe import is_keyframe_clip_id, persist_keyframe_clip_time
                if is_keyframe_clip_id(c[0]):
                    persist_keyframe_clip_time(cur, c)
                    continue
                if is_caption_id(c[0]):
                    cur.execute(
                        "UPDATE captions SET start_frame = ?, length_frames = ? WHERE id = ?",
                        (c[2], c[3], -c[0])
                    )
                    continue
                cur.execute(
                    "UPDATE clips SET start_frame = ?, length_frames = ?, in_frame = ? "
                    "WHERE id = ?",
                    (c[2], c[3], c[7], c[0])
                )
            conn.commit()
            conn.close()
            # Preview: old and new ranges of trimmed video/image/keyframe
            # clips and linked partners (re-marked after the reload below)
            self._mark_preview_dirty(self._move_preview_ranges(group, drag))
        except Exception as e:
            QMessageBox.warning(self, "Clip Edge", f"Could not save clip: {e}")
        # The video clip of the group gets its out icon (dragged or partner)
        video_members = [m["clip"] for m in group if self._clip_is_video(m["clip"])]
        if video_members:
            clip = video_members[0]
        track_id = self._clip_track_id(clip)
        is_video = bool(video_members)
        self.refresh()
        if drag["edge"] == "right" and is_video:
            # clip is the live list edited by the drag, so clip[3] is the new
            # length. Out frame = in_frame + length_frames - 1
            out_frame = out_frame_for(clip[7], clip[3])
            # print(f"ICON out release clip={clip[0]} in_frame={clip[7]} length={clip[3]} "
                  # f"out_frame={out_frame} track_height={self._track_row_height(track_id)}")
            self._generate_out_icon(clip[1], track_id, out_frame)
        self.table.viewport().update()
        self.ruler.viewport().update()
        self._refresh_sequence_manager()
        # Length may have changed; keep frame and view inside it
        self._clamp_view()
        self.current_frame = max(0, min(self.total_frames() - 1, self.current_frame))
        self._save_view_state()
        self.status_bar.showMessage(f"Clip start {clip[2]}, length {clip[3]}", 3000)

    def _clip_track_id(self, clip):
        for track_id, clips in self.clips_by_track.items():
            for c in clips:
                if c[0] == clip[0]:
                    return track_id
        return None

    def _on_section_resized(self, index, old_size, new_size):
        if index != CLIPS_COL:
            self._update_clips_width()
        if index == 0:
            self._save_name_width()

    def _save_name_width(self):
        # Name column width in the per-sequence view state (globalsettings)
        if not self._view_state_ready or self.sequence_id is None:
            return
        _set_global_setting(self._view_key("namewidth"),
                            str(int(self.ruler.sectionSize(0))))

    def _restore_name_width(self):
        try:
            w = int(_get_global_setting(self._view_key("namewidth"), "") or "")
        except ValueError:
            return
        if w > 0:
            self.ruler.resizeSection(0, w)

    def zoom_in(self):
        if self.pixels_per_frame < ZOOM_MAX_PPF:
            self.pixels_per_frame = clamp_zoom(self.pixels_per_frame * ZOOM_STEP)
            self._update_clips_width()
            self._update_status()
            self._save_view_state()

    def zoom_out(self):
        if self.pixels_per_frame > ZOOM_MIN_PPF:
            self.pixels_per_frame = clamp_zoom(self.pixels_per_frame / ZOOM_STEP)
            self._update_clips_width()
            self._update_status()
            self._save_view_state()

    def set_current_frame(self, frame, silent=False):
        frame = max(0, min(self.total_frames() - 1, int(frame)))
        if frame != self.current_frame:
            self.current_frame = frame
            self.ruler.viewport().update()
            self.playhead_overlay.update()
        if not silent:
            if self._playing:
                import time
                self._play_from = frame
                self._play_start_frame = frame
                self._play_start_time = time.perf_counter()
                self._play_cursor = frame
                self._play_audio_clock = False
                player = getattr(self, "_player", None)
                if player is not None and not getattr(player, "closed", False):
                    try:
                        player.seek_preview_audio(frame)
                    except Exception:
                        pass
            self._update_status()

    def refresh(self):
        # Table and clips reload (sequence_timeline_sql.py), then the player
        refresh(self)
        from markers import load_markers
        self._markers = load_markers(self._project_db())
        self._update_player()
        # Edit: new snapshot, wake the preview worker
        self._retarget_preview(force=True)

    def _apply_controls_alignment(self):
        # Global setting playercontrols_alignment: left (default), center, right.
        # Row items: [left stretch, 5 buttons, right stretch]
        align = (_get_global_setting("playercontrols_alignment", "left") or "left").lower()
        left, right = {"center": (1, 1), "right": (1, 0)}.get(align, (0, 1))
        row = self._controls_row
        row.setStretch(0, left)
        row.setStretch(row.count() - 1, right)

    def _edit_points(self):
        # Every clip in-point and out-point in the sequence (all tracks and
        # clip types, enabled or not)
        points = set()
        for clips in self.clips_by_track.values():
            for clip in clips:
                points.add(int(clip[2]))
                points.add(int(clip[2]) + int(clip[3]))
        # Marker frames are stops too
        from markers import marker_frames
        points.update(marker_frames(getattr(self, "_markers", None), self.clips_by_track))
        return sorted(points)

    def _jump_to_frame(self, frame):
        # Scroll so the frame is visible, then the ruler's scrub path
        visible = self.visible_frames()
        if not (self.view_start_frame <= frame < self.view_start_frame + visible):
            self.view_start_frame = max(0, frame - visible // 2)
            self._clamp_view()
            self.table.viewport().update()
        self.set_current_frame(frame)

    def go_prev_edit(self):
        # |<: previous jump target; none before, wrap to the last target
        from edit_points import jump_targets, prev_edit_target
        targets = jump_targets(self._edit_points(), self.total_frames())
        self._jump_to_frame(prev_edit_target(targets, self.current_frame))

    def go_next_edit(self):
        # >|: next jump target; none after, wrap to 0
        from edit_points import jump_targets, next_edit_target
        targets = jump_targets(self._edit_points(), self.total_frames())
        self._jump_to_frame(next_edit_target(targets, self.current_frame))

    def _on_play_control(self, name):
        # Home and End use the Home/End key path (playhead, view, scrub hook).
        # The rest are stubs: playback, speed and edit jumps are not wired yet.
        if name == "Jump to home":
            self.go_home()
        elif name == "Jump to end":
            self.go_end()
        elif name == "Jump to previous edit":
            self.go_prev_edit()
        elif name == "Jump to next edit":
            self.go_next_edit()
        elif name == "Play":
            self._begin_play()
        elif name == "Pause":
            self._stop_play()
        else:
            print(name)

    def _toggle_play(self):
        # Same as the Pause button while playing or filling, else Play
        if self._playing or self._play_filling:
            self._on_play_control("Pause")
        else:
            self._on_play_control("Play")

    def _begin_play(self):
        # Fill every missing preview frame from the tick to the end, then
        # run the clock. The player only reads the preview file.
        total = self.total_frames()
        if total <= 0 or self.sequence_id is None:
            return
        self._stop_play()
        try:
            from sequence_player import open_player_for
            open_player_for(self)
        except Exception as e:
            print(f"Play could not open the player: {e}")
        self._play_from = int(self.current_frame)
        if self._play_ready():
            self._start_clock()
            return
        self._play_filling = True
        try:
            from sequence_preview_worker import set_play_priority
            set_play_priority(True)
        except Exception as e:
            print(f"Play priority failed: {e}")
        self._retarget_preview(force=True)
        self._play_fill_status()

    def _play_ready(self):
        # True when every frame from the tick to the end is a clean preview
        try:
            from sequence_preview import clean_set
            clean = clean_set(self._project_db(), self.sequence_id)
        except Exception:
            return False
        total = self.total_frames()
        for frame in range(int(self._play_from), total):
            if frame not in clean:
                return False
        return True

    def _play_fill_status(self):
        try:
            from sequence_preview import clean_set
            clean = clean_set(self._project_db(), self.sequence_id)
            left = sum(1 for f in range(int(self._play_from), self.total_frames())
                       if f not in clean)
        except Exception:
            left = 0
        self.status_bar.showMessage(f"Filling preview: {left} frames left")

    def _start_clock(self):
        # Prime the start-frame batch, then audio, then the tick timer.
        # Do not start the WAV until play_has(start) is true.
        self._play_filling = False
        try:
            from sequence_preview_worker import set_play_priority
            set_play_priority(False)
        except Exception:
            pass
        self._play_start_frame = int(self._play_from)
        self._play_start_time = 0.0
        self._play_cursor = int(self._play_from)
        self._play_shown_count = 0
        self._play_last_shown = None
        self._play_audio_clock = False
        self._play_prime_tries = 0
        self._playing = True
        self._play_timer.stop()
        self._play_prime_once()

    def _play_prime_once(self):
        # (1) play_fill start (2) wait for play_has (3) start_preview_audio
        # (4) start the play timer. Retries cover a new player still in _setup.
        if not self._playing:
            return
        player = getattr(self, "_player", None)
        if player is None or getattr(player, "closed", False):
            self._stop_play()
            return
        start = int(self._play_start_frame)
        max_tries = 100  # 100 * 20ms ~= 2s for GL init + first nvJPEG batch
        ready = bool(getattr(player, "ready", False))
        if ready:
            try:
                if not player.play_has(start):
                    player.play_fill(start, self._play_batch_n())
            except Exception as e:
                # print("play prime fill failed: %s" % e)
                pass
        has = False
        if ready:
            try:
                has = bool(player.play_has(start))
            except Exception:
                has = False
        if has:
            import time
            self._play_start_time = time.perf_counter()
            try:
                player.start_preview_audio(start)
            except Exception as e:
                # print("preview audio start failed: %s" % e)
                pass
            self._play_timer.setInterval(10)
            self._play_timer.start()
            self._play_tick()
            return
        self._play_prime_tries += 1
        if self._play_prime_tries >= max_tries:
            # Bounded give-up: start anyway so Play is not stuck forever.
            import time
            self._play_start_time = time.perf_counter()
            try:
                player.start_preview_audio(start)
            except Exception:
                pass
            self._play_timer.setInterval(10)
            self._play_timer.start()
            self._play_tick()
            return
        QTimer.singleShot(20, self._play_prime_once)

    def _play_batch_n(self):
        try:
            n = int(_get_global_setting("previewGpuBatchFrames", "8") or 8)
        except Exception:
            n = 8
        return max(1, min(30, n))

    def _play_tick(self):
        # After prime, audio position is the master clock once the first
        # frame has been shown; wall clock is the fallback. Keep filling
        # batches ahead of the due frame. If the exact cursor miss-stalls,
        # fill at the due frame and show the newest available <= due.
        import time
        if not self._playing:
            return
        player = getattr(self, "_player", None)
        if player is None or player.closed:
            self._stop_play()
            return
        start = int(self._play_start_frame)
        clock = None
        if self._play_audio_clock:
            try:
                pos = player.preview_audio_position_ms()
            except Exception:
                pos = None
            if pos is not None and int(pos) > 0:
                try:
                    from sequence_player_audio import sequence_frame_from_position_ms
                    clock = sequence_frame_from_position_ms(pos, self.fps)
                except Exception:
                    clock = None
        if clock is None:
            if not self._play_audio_clock:
                clock = start
            else:
                elapsed = time.perf_counter() - self._play_start_time
                clock = self._play_start_frame + int(elapsed * self.fps)
        last = self.total_frames() - 1
        if clock < 0:
            clock = 0
        if clock > last:
            clock = last
        due = int(clock)
        batch = self._play_batch_n()

        # Fill-ahead: top up the queue toward due + one batch
        try:
            hi = player.play_queue_hi()
        except Exception:
            hi = None
        fill_from = int(self._play_cursor) if hi is None else int(hi) + 1
        need = due + batch
        if fill_from <= need and fill_from <= last:
            if hi is None or hi < need:
                try:
                    player.play_fill(fill_from, batch)
                except Exception:
                    pass

        if self._play_cursor > due:
            return

        shown = None
        if player.play_has(self._play_cursor):
            if player.play_show(self._play_cursor):
                shown = int(self._play_cursor)
        if shown is None:
            # Exact cursor missing or show failed. Do not wedge: fill at
            # due and present the newest decoded frame at or below due.
            if due >= self._play_cursor:
                try:
                    if not player.play_has(due):
                        player.play_fill(due, batch)
                except Exception:
                    pass
                try:
                    shown = player.play_show_at_most(due)
                except Exception:
                    shown = None
            if shown is None:
                # Skip a hole so the next tick can refill ahead of audio
                if self._play_audio_clock and due - self._play_cursor > 2:
                    try:
                        player.play_drop_before(due)
                    except Exception:
                        pass
                    self._play_cursor = due
                return

        self._play_cursor = int(shown)
        self._play_audio_clock = True
        self.set_current_frame(self._play_cursor, silent=True)
        visible = self.visible_frames()
        if not (self.view_start_frame <= self._play_cursor <
                self.view_start_frame + visible):
            self.view_start_frame = max(0, self._play_cursor - visible // 4)
            self._clamp_view()
            self.table.viewport().update()
        self._play_shown_count += 1
        self._play_status()
        self._play_cursor += 1
        # Prebuffer the next batch while the just-shown frame is on screen
        try:
            if self._play_cursor <= last and not player.play_has(self._play_cursor):
                player.play_fill(self._play_cursor, batch)
        except Exception:
            pass
        if self._play_cursor > last:
            self._stop_play()

    def _stop_play(self):
        was = self._playing or self._play_filling
        self._playing = False
        self._play_filling = False
        self._play_prime_tries = 0
        self._play_timer.stop()
        try:
            from sequence_preview_worker import set_play_priority
            set_play_priority(False)
        except Exception:
            pass
        if was:
            self._save_view_state()
        player = getattr(self, "_player", None)
        if player is not None and not player.closed:
            player.stop_read_ahead()
            try:
                player.stop_preview_audio()
            except Exception:
                pass

    def open_player(self):
        # Player menu: open this timeline's Sequence Player, or raise it
        try:
            from sequence_player import open_player_for
            open_player_for(self)
        except Exception as e:
            QMessageBox.warning(self, "Player", f"Could not open the player: {e}")

    def _play_rate_text(self):
        # Clock rate is frames the playhead advanced per wall second.
        # Shown rate is distinct preview pictures actually presented.
        if not self._playing:
            return ""
        import time
        elapsed = time.perf_counter() - self._play_start_time
        if elapsed < 0.2:
            return "  Playing"
        clock = (self.current_frame - self._play_start_frame) / elapsed
        shown = self._play_shown_count / elapsed
        return f"  Playing clock {clock:.2f}  shown {shown:.2f}"

    def _play_status(self):
        # Status text only. No scrub, no preview retarget.
        seconds = self.current_frame / self.fps
        m = int(seconds // 60)
        s = seconds - m * 60
        self.status_bar.showMessage(
            f"Frame {self.current_frame}  Time {m:02d}:{s:05.2f}  "
            f"View from {self.view_start_frame}  "
            f"FPS {self.fps:g}  Zoom {self.pixels_per_frame:.3g}px/frame"
            f"{self._play_rate_text()}"
        )
        # Clock rate is frames the playhead advanced per wall second.
        # Shown rate is distinct preview pictures actually presented.
        if not self._playing:
            return ""
        import time
        elapsed = time.perf_counter() - self._play_start_time
        if elapsed < 0.2:
            return "  Playing"
        clock = (self.current_frame - self._play_start_frame) / elapsed
        shown = self._play_shown_count / elapsed
        return f"  Playing clock {clock:.2f}  shown {shown:.2f}"

    def _update_player(self):
        # Show the frame under the scrub in the open player, if any
        player = getattr(self, "_player", None)
        if player is not None and not player.closed:
            player.request_frame()

    def _update_status(self):
        seconds = self.current_frame / self.fps
        m = int(seconds // 60)
        s = seconds - m * 60
        self.status_bar.showMessage(
            f"Frame {self.current_frame}  Time {m:02d}:{s:05.2f}  "
            f"View from {self.view_start_frame}  "
            f"FPS {self.fps:g}  Zoom {self.pixels_per_frame:.3g}px/frame"
            f"{self._play_rate_text()}"
        )
        # Scrub moved: the player seeks (skipped if the frame is unchanged)
        player = getattr(self, "_player", None)
        if player is not None and not player.closed:
            # Timing debug: when the scrub hook fired
            import time
            player._t_hook = time.perf_counter()
            player.request_frame(force=False)
            # Main-thread generate stalled the UI; the worker fills it instead
            # self._fill_preview_frame()
        # Playhead moved: the preview worker works outward from here.
        # Not during playback; that would restart the worker every frame.
        if not self._playing and not self._play_filling:
            self._retarget_preview()

    def _retarget_preview(self, force=False):
        # Point the single preview worker at this timeline. GUI thread only:
        # the snapshot copies the rows and clips the worker composites.
        if self.sequence_id is None or not hasattr(self, "table"):
            return
        frame = int(self.current_frame)
        try:
            # Any timeline action keeps the worker off the decoder for a moment
            from sequence_player import gui_touch
            gui_touch()
        except Exception:
            pass
        if not force and frame == getattr(self, "_preview_center", None):
            return
        try:
            from sequence_preview_worker import get_worker
            from sequence_player import timeline_snapshot
            # Snapshot only on edit, activation or first use; a scrub reuses it
            snap = getattr(self, "_preview_snapshot", None)
            if force or snap is None:
                snap = timeline_snapshot(self)
                self._preview_snapshot = snap
            get_worker().retarget(self._project_db(), self.sequence_id, frame,
                                  self.total_frames(), snap,
                                  getattr(self, "fps", None))
            self._preview_center = frame
            pending = getattr(self, "_preview_pending_shift", None)
            if force and pending:
                # After the retarget, so the worker's old-snapshot pass
                # cannot start a new chunk; a chunk already rendering was
                # numbered before this shift and stays dirty
                self._preview_pending_shift = None
                try:
                    from sequence_preview import FramesShift
                    first, last, delta = pending[0]
                    FramesShift(self._project_db(), self.sequence_id,
                                first, last, delta)
                except Exception as e:
                    # print(f"Preview shift failed: {e}")
                    self._mark_preview_dirty(pending[1])
            remark = getattr(self, "_preview_remark", None)
            if force and remark:
                self._preview_remark = []
                from sequence_preview import FramesMarkDirty
                FramesMarkDirty(self._project_db(), self.sequence_id, remark)
        except Exception as e:
            print(f"Preview worker retarget failed: {e}")

    def _preview_tick(self):
        # Ruler repaint only when the worker saved rows for this timeline
        try:
            from sequence_preview_worker import _worker
        except Exception:
            return
        if _worker is None or _worker.sequence_id != self.sequence_id:
            return
        if _worker.saved != self._preview_seen:
            self._preview_seen = _worker.saved
            self.ruler.viewport().update()
        if self._play_filling:
            self._play_fill_status()
            if self._play_ready():
                self._start_clock()

    def changeEvent(self, event):
        # Active timeline gets the preview worker
        super().changeEvent(event)
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._retarget_preview(force=True)

    def _fill_preview_frame(self):
        # Playhead moved: cache the frame if the preview db has no clean row,
        # so the preview bar turns green. The player still shows the live path.
        if self.sequence_id is None:
            return
        try:
            from sequence_preview import FrameRetrieve, FrameSave
            from sequence_player import FrameGenerate
            project = self._project_db()
            frame = int(self.current_frame)
            if FrameRetrieve(project, self.sequence_id, frame) is not None:
                return
            # Miss: wake the worker too (it yields while this generate runs)
            self._retarget_preview(force=True)
            jpeg = FrameGenerate(project, self.sequence_id, frame)
            if jpeg is None:
                return
            FrameSave(project, self.sequence_id, frame, jpeg, 0)
            self.ruler.viewport().update()
        except Exception as e:
            print(f"Preview cache fill failed: {e}")

    def _stub(self, label):
        QMessageBox.information(self, label, f"{label} stub for sequence id {self.sequence_id}")

    def _view_key(self, name):
        return f"SequenceTimeline_{self.sequence_id}_{name}"

    def _save_view_state(self):
        # Per-sequence zoom, current frame and view start in globalsettings
        if not self._view_state_ready or self.sequence_id is None:
            return
        _set_global_setting(self._view_key("zoom"), repr(float(self.pixels_per_frame)))
        _set_global_setting(self._view_key("frame"), str(int(self.current_frame)))
        _set_global_setting(self._view_key("view"), str(int(self.view_start_frame)))
        self._save_name_width()

    def _restore_view_state(self):
        # Called in __init__ once fps and length are known, before the first paint
        if self.sequence_id is None:
            return

        def read_int(name, default):
            try:
                return int(_get_global_setting(self._view_key(name), "") or "")
            except ValueError:
                return default

        total = max(1, int(round(self.length_seconds * self.fps)))
        try:
            zoom = float(_get_global_setting(self._view_key("zoom"), "") or "")
        except ValueError:
            zoom = self.pixels_per_frame
        self.pixels_per_frame = clamp_zoom(zoom)
        self.current_frame = max(0, min(total - 1, read_int("frame", 0)))
        self.view_start_frame = max(0, min(total - 1, read_int("view", 0)))

    def _restore_geometry(self):
        try:
            x = int(_get_global_setting(f"SequenceTimeline_{self.sequence_id}_x", "") or "")
            y = int(_get_global_setting(f"SequenceTimeline_{self.sequence_id}_y", "") or "")
            w = int(_get_global_setting(f"SequenceTimeline_{self.sequence_id}_w", "") or "")
            h = int(_get_global_setting(f"SequenceTimeline_{self.sequence_id}_h", "") or "")
        except ValueError:
            return
        if w > 0 and h > 0:
            self.setGeometry(x, y, w, h)

    def _save_geometry(self):
        if not self._geometry_ready:
            return
        g = self.geometry()
        _set_global_setting(f"SequenceTimeline_{self.sequence_id}_x", str(g.x()))
        _set_global_setting(f"SequenceTimeline_{self.sequence_id}_y", str(g.y()))
        _set_global_setting(f"SequenceTimeline_{self.sequence_id}_w", str(g.width()))
        _set_global_setting(f"SequenceTimeline_{self.sequence_id}_h", str(g.height()))

    def moveEvent(self, event):
        super().moveEvent(event)
        self._save_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "ruler"):
            self._update_clips_width()
        self._save_geometry()

    def showEvent(self, event):
        super().showEvent(event)
        if self.sequence_id is not None:
            _add_open_id(self.sequence_id)
        self._apply_controls_alignment()
        # Preview bar from the existing {id}_preview.db: paint once on open
        if hasattr(self, "ruler"):
            self.ruler.viewport().update()

    def closeEvent(self, event):
        # Always save geometry and view state first
        self._save_geometry()
        self._save_view_state()
        # Preview worker stops working on this timeline
        try:
            from sequence_preview_worker import _worker
            if _worker is not None:
                _worker.clear(self.sequence_id)
        except Exception:
            pass
        if hasattr(self, "_preview_timer"):
            self._preview_timer.stop()
        # The player closes with its timeline
        if getattr(self, "_player", None) is not None:
            from sequence_player import close_player_for
            close_player_for(self)
        if self.sequence_id is not None and not app_quitting:
            # User closed this one window: forget it
            _remove_open_id(self.sequence_id)
            if _open_windows.get(self.sequence_id) is self:
                del _open_windows[self.sequence_id]
        event.accept()

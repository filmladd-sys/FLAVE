# sequence_timeline.py
# Sequence Timeline window for Ladd Video Editor
# Track table with a frame ruler and scrubber in the Clips header.
# ASCII only.

import math
import os
import sqlite3
from sequence_timeline_ruler import (
    CLIPS_COL, RULER_HEIGHT, ZOOM_MIN_PPF, ZOOM_MAX_PPF, ZOOM_STEP, WHEEL_ZOOM_STEP,
    RulerHeader, clamp_zoom, ruler_label_interval, ruler_label_text,
    PlayheadOverlay, view_max_start, view_min_start,
)
from sequence_timeline_image import IMAGE_EXTENSIONS, _add_image_track
from sequence_timeline_keyframe import _add_keyframe_track
from sequence_timeline_video import (
    VIDEO_EXTENSIONS, out_frame_for, video_out_pixmap,
    _add_video_track, _add_video_audio_track, _generate_out_icon, _clip_is_video,
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
    DEFAULT_TRACK_HEIGHT, COLLAPSE_COL, COLLAPSE_COL_WIDTH, COLLAPSED_ROW_HEIGHT,
    ROLE_COLLAPSED,
    ROLE_OPEN_HEIGHT, CLIP_TRACK_TYPES, EDIT_TRACK_TYPES, TRACK_FOOTAGE_EXTENSIONS,
    _global_db_path, _get_global_setting, _set_global_setting, _read_open_ids,
    _write_open_ids, _add_open_id, _remove_open_id, get_open_timeline_ids,
    _write_open_player_ids, get_open_player_ids,
    sequence_exists, _load_sequence_name, ensure_tracks_table, ensure_clips_table,
    _load_sequence_duration, _latest_clip_end, sequence_length_seconds, _load_sequence_fps,
    _add_track, refresh, _save_track_display, _move_track, _move_track_to,
    _toggle_track_flag, drop_footage, _range_overlaps, delete_selected_clip,
    drop_sequence, sequence_id_from_mime, delete_track,
    slice_clip, ripple_delete, _toggle_clip_flag,
    ripple_delete_clips,
)
# BUILD 161: track parts of the window (same names, re-exported here)
from sequence_timeline_tracks import (
    _TRACK_STRIP_STATES, _plain_option, _NoTrackHighlightStyle, _track_table_style,
    paint_collapse_triangle, paint_collapse_cell, TrackDisplayDialog,
    TRACK_ADD_ITEMS, _track_add_items, _add_track_actions, _add_track_menu_items,
    _run_track_menu_action, _track_display_properties, _track_row_height,
    _toggle_collapse_at, _setup_collapse_column, NameCellDelegate,
    _track_name_click,
)
# BUILD 162: clip parts of the window (same names, re-exported here)
from sequence_timeline_clips import (
    VIDEO_CLIP_BG, AUDIO_CLIP_BG, ClipDelegate, _SEQ_ICON_CACHE,
    COLLAPSED_BAR_COLORS, NAME_MIN_PX, draw_clip_name, paint_collapsed_clip,
    moving_marker_frames, sequence_clip_icon, _linked_clips, _clip_edge_at,
    _clip_by_id, _clear_selection, _select_clip, _selected_clips, _drag_group,
    _edge_drag_move, _snap_pixels, _snap_drag_delta, _set_snap_frame,
    _group_delta_range, _neighbor_bounds, _clip_image_track_row, _clip_at,
    _move_target_track, _relocate_clip, _cross_track_preview,
    _move_drag_release, _move_preview_ranges, _preview_shift_plan,
    _ranges_for_clips, _clip_context_menu, _edge_drag_release, _clip_track_id,
    _clip_press_select, _clip_press_drag, _clip_edge_cursor,
    _clip_drag_release,
)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QStatusBar, QMessageBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QInputDialog, QAbstractItemView, QMenu, QApplication,
    QStyledItemDelegate, QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QCheckBox, QLineEdit, QLabel, QColorDialog, QDialogButtonBox,
    QProxyStyle, QStyle, QStyleFactory
)
from PySide6.QtCore import Qt, QRect, QEvent, QTimer
from PySide6.QtGui import (
    QAction, QPainter, QColor, QPen, QShortcut, QKeySequence, QPixmap
)


# BUILD 138: debounce after the last completed edit before the audio state
# check (which also starts the background mix); quick edits -> one mix
AUDIO_SETTLE_MS = 500
# BUILD 244: Play A/V lock. Audio position is the master clock. The tick
# used to show one queued frame per tick (the frame after the last one
# shown), so when ticks plus present ran slower than the frame rate the
# video fell behind the audio and never caught up. Now: (1) each tick, if
# the next frame is behind the due frame and the due frame is decoded, it
# jumps to the due frame (older queued frames dropped); (2) every
# RESYNC_INTERVAL_S, if the next frame is still behind the due frame, the
# cursor is moved to the due frame even when it is not decoded yet (the
# tick then fills at due). Ahead of the clock -> the current frame is held
# until the clock reaches the next one (no stepping back, no decode).
RESYNC_INTERVAL_S = 1.0

# BUILD 158 no-track-highlight style (_TRACK_STRIP_STATES, _plain_option,
# _NoTrackHighlightStyle, _track_table_style): BUILD 161 moved it to
# sequence_timeline_tracks.py (imported above)


# BUILD 161: paint_collapse_triangle -> sequence_timeline_tracks.py


# BUILD 161: TrackDisplayDialog -> sequence_timeline_tracks.py


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
    # BUILD 229: under the opener's menu bar on first open / monitor change
    # / off-screen (window_place); memory per sequence like the geometry
    from window_place import place_under_parent
    place_under_parent(win, parent, "SequenceTimeline_%d" % int(sequence_id),
                       ("SequenceTimeline_%d_w" % int(sequence_id),))
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
    _add_video_audio_track = _add_video_audio_track
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
    _move_track_to = _move_track_to   # BUILD 124: Move > To Top / To Bottom
    _toggle_track_flag = _toggle_track_flag
    drop_footage = drop_footage
    drop_sequence = drop_sequence
    delete_track = delete_track
    _range_overlaps = _range_overlaps
    delete_selected_clip = delete_selected_clip
    slice_clip = slice_clip
    ripple_delete = ripple_delete
    ripple_delete_clips = ripple_delete_clips   # BUILD 245: clip menu
    _toggle_clip_flag = _toggle_clip_flag
    # BUILD 161: track style / collapse / menus / row height live in
    # sequence_timeline_tracks.py
    TRACK_ADD_ITEMS = TRACK_ADD_ITEMS
    _track_add_items = _track_add_items
    _add_track_actions = _add_track_actions
    _add_track_menu_items = _add_track_menu_items
    _run_track_menu_action = _run_track_menu_action
    _track_display_properties = _track_display_properties
    _track_row_height = _track_row_height
    _toggle_collapse_at = _toggle_collapse_at
    _setup_collapse_column = _setup_collapse_column
    _track_name_click = _track_name_click
    # BUILD 162: clip selection / drag / trim / menu live in
    # sequence_timeline_clips.py
    _linked_clips = _linked_clips
    _clip_edge_at = _clip_edge_at
    _clip_by_id = _clip_by_id
    _clear_selection = _clear_selection
    _select_clip = _select_clip
    _selected_clips = _selected_clips
    _drag_group = _drag_group
    _edge_drag_move = _edge_drag_move
    _snap_pixels = _snap_pixels
    _snap_drag_delta = _snap_drag_delta
    _set_snap_frame = _set_snap_frame
    _group_delta_range = _group_delta_range
    _neighbor_bounds = _neighbor_bounds
    _clip_image_track_row = _clip_image_track_row
    _clip_at = _clip_at
    _move_target_track = _move_target_track
    _relocate_clip = _relocate_clip
    _cross_track_preview = _cross_track_preview
    _move_drag_release = _move_drag_release
    _move_preview_ranges = _move_preview_ranges
    _preview_shift_plan = _preview_shift_plan
    _ranges_for_clips = _ranges_for_clips
    _clip_context_menu = _clip_context_menu
    _edge_drag_release = _edge_drag_release
    _clip_track_id = _clip_track_id
    _clip_press_select = _clip_press_select
    _clip_press_drag = _clip_press_drag
    _clip_edge_cursor = _clip_edge_cursor
    _clip_drag_release = _clip_drag_release

    def __init__(self, parent=None, sequence_id=None):
        super().__init__(parent)
        # BUILD 132: state read by eventFilter / the delegate / the overlay /
        # the slice, marker, key and drop-preview modules, set before any
        # widget exists. The viewport filter is installed mid-__init__ and
        # Qt sends it events (setAcceptDrops, setMouseTracking, Resize ...)
        # before the rest of __init__ runs. Later assignments below re-set
        # the same defaults.
        self.sequence_id = sequence_id
        self._slice_mode = False      # BUILD 131 slice mode on
        self._slice_lines = []        # BUILD 131 yellow line rects (viewport)
        self._edge_drag = None
        self._key_drag = None
        self._marker_drag = None
        self._hover_marker_id = None
        self._snap_frame = None
        self._drop_info = None        # BUILD 125 drop preview
        self._drop_preview = []
        self._rubber = None           # BUILD 148 rubber band (armed / drag)
        self._rubber_rect = None      # BUILD 148 box drawn by the overlay
        # self._rubber_eat_menu = False   # BUILD 148 (right button), unused
        self.clips_by_track = {}
        self._selected_clip_ids = set()
        self._selection_order = []
        self._selection_solo = False
        self._icon_cache = {}
        self._icon_miss = {}      # icon_pixmap misses, see icon_pixmap
        self._icon_miss_serial = None
        self._peaks_cache = {}
        self._out_paint_miss = {}
        self._seq_refs = {}
        self._markers = {}
        self._filling = False
        self._playing = False
        self._play_filling = False
        self._preview_center = None
        self._preview_seen = -1
        self._preview_status = None   # BUILD 129 label, created below
        self._play_mixing = False     # BUILD 136: Play waits for the audio mix
        self._audio_mix_job = None    # BUILD 136: sequence_preview_audio.MixJob
        self._audio_mix_timer = None  # BUILD 136: GUI-thread poll of the job
        self._audio_bar_timer = None  # BUILD 137: debounced audio state check
        self._audio_auto_failed = False  # BUILD 138: no auto retry until an edit / Play
        self._audio_auto_wanted = False  # BUILD 138: auto mix waits for the global slot
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
        # BUILD 126: 4th logical column = collapse triangle, shown far left
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "", ""])
        # self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # BUILD 158: no track highlight. No row selection at all (nothing
        # reads the table selection: menus / Name edit use the clicked row)
        # and the cell panels drop Selected / hover / focus states.
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setStyle(_track_table_style(self.table))
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
        # BUILD 161: triangle column / collapsed row sizes:
        # sequence_timeline_tracks._setup_collapse_column
        self._setup_collapse_column()
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
        self._control_buttons = {}   # BUILD 163: name -> button (active cue)
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
            self._control_buttons[name] = btn
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
        self._icon_miss = {}
        self._icon_miss_serial = None
        self._peaks_cache = {}   # footage_id -> (pps, duration, array) or None
        # Clip selection: ids for painting, ordered list so "first" is the
        # first clip selected. Selection solo (Alt+click) is in memory only;
        # it is not the clips.solo DB column.
        self._selected_clip_ids = set()
        self._selection_order = []
        self._selection_solo = False
        self._out_paint_miss = {}   # clip_id -> (frame, height) last printed miss
        self.table.setItemDelegateForColumn(CLIPS_COL, ClipDelegate(self))
        self.table.setItemDelegateForColumn(COLLAPSE_COL, ClipDelegate(self))
        # BUILD 163: Name cell outline while the track menu is open
        self._menu_track_row = -1
        # Track selection (Name cell click / Shift+click): track ids
        self._selected_track_ids = set()
        self._track_anchor_id = None
        self.table.setItemDelegateForColumn(0, NameCellDelegate(self))
        self.table.setAcceptDrops(True)
        self.table.viewport().setAcceptDrops(True)
        # Clip edge dragging: hover cursor needs mouse tracking
        self.table.viewport().setMouseTracking(True)
        self._edge_drag = None
        self._key_drag = None
        self._snap_frame = None   # white snap line frame, None = hidden
        self._insert_frame = None   # Shift+drag insert cut line frame

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        # Right-click on the status bar: Copy puts the current message on
        # the clipboard (read at the click, before it can time out)
        self.status_bar.setContextMenuPolicy(Qt.CustomContextMenu)
        self.status_bar.customContextMenuRequested.connect(
            self._status_bar_menu)
        # BUILD 129: 'Generating preview NN%' in a permanent label at the
        # right of the status bar (showMessage, the play clock / frame text,
        # is untouched). Polled on the GUI thread at 4 per second.
        self._preview_status = QLabel("")
        self.status_bar.addPermanentWidget(self._preview_status)
        self._preview_status_timer = QTimer(self)
        self._preview_status_timer.setInterval(250)
        self._preview_status_timer.timeout.connect(self._preview_status_poll)
        self._preview_status_timer.start()

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
        # BUILD 131: slice mode. C enters, V exits (window shortcuts: a text
        # field or item editor takes plain letters first via shortcut
        # override); Escape exits in keyPressEvent; right-click exits in
        # the viewport filter
        self._slice_mode = False
        self._slice_lines = []
        QShortcut(QKeySequence(Qt.Key_C), self, activated=self._slice_enter)
        QShortcut(QKeySequence(Qt.Key_V), self, activated=self._slice_exit_key)
        # BUILD 156: Ctrl+C / Ctrl+X / Ctrl+V = Copy / Cut / Paste To Tic
        # (window shortcuts; a text field / spin box / item editor takes
        # its own copy / cut / paste first via shortcut override, and the
        # handlers also do nothing while one has focus). Not tied to slice
        # mode (plain C / V stay the slice keys).
        QShortcut(QKeySequence("Ctrl+C"), self, activated=self._key_copy)
        QShortcut(QKeySequence("Ctrl+X"), self, activated=self._key_cut)
        QShortcut(QKeySequence("Ctrl+V"), self, activated=self._key_paste)
        # BUILD 166: F5 = Refresh Preview (dirty video frames + audio mix)
        QShortcut(QKeySequence(Qt.Key_F5), self, activated=self.refresh_preview)
        # BUILD 280: Ctrl+F = Edit > Find... (captions); F3 / Shift+F3 =
        # next / previous match
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._caption_find)
        QShortcut(QKeySequence(Qt.Key_F3), self, activated=self._caption_find_next)
        QShortcut(QKeySequence("Shift+F3"), self, activated=self._caption_find_prev)

        self._create_menus()
        # BUILD 281: activating this window selects its sequence; its
        # Sequence Player stays in front of the other FLAVE windows
        from player_front import install as _player_front_install
        _player_front_install()
        self.refresh()
        self._update_clips_width()
        self._update_status()
        self._restore_geometry()
        self._geometry_ready = True
        self._view_state_ready = True

    def _create_menus(self):
        menubar = self.menuBar()

        # File menu: Exit quits FLAVE the same way as Project Manager's
        # File > Exit (its closeEvent saves the open windows, then quits)
        file_menu = menubar.addMenu("File")
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self._exit_app)
        file_menu.addAction(exit_action)

        # Edit menu (in-memory clipboard per sequence, timeline_clipboard.py)
        edit_menu = menubar.addMenu("Edit")
        # BUILD 156: the shortcut text only (the keys are the QShortcuts
        # above; a second binding on the action would make them ambiguous)
        self._copy_action = QAction("Copy\tCtrl+C", self)
        self._copy_action.triggered.connect(self._clip_copy)
        self._paste_tic_action = QAction("Paste To Tic\tCtrl+V", self)
        self._paste_tic_action.triggered.connect(self._clip_paste_tic)
        self._paste_end_action = QAction("Paste to End", self)
        self._paste_end_action.triggered.connect(self._clip_paste_end)
        self._cut_action = QAction("Cut\tCtrl+X", self)
        self._cut_action.triggered.connect(self._clip_cut)
        for a in (self._copy_action, self._paste_tic_action,
                  self._paste_end_action, self._cut_action):
            edit_menu.addAction(a)
        # BUILD 280: Find in captions (sequence_timeline_find.py); the keys
        # are the QShortcuts above, shown as text only
        edit_menu.addSeparator()
        find_action = QAction("Find...\tCtrl+F", self)
        find_action.triggered.connect(lambda checked=False: self._caption_find())
        edit_menu.addAction(find_action)
        edit_menu.aboutToShow.connect(self._update_edit_actions)
        self._update_edit_actions()

        # Tracks menu (BUILD 123: same items / handlers as the right-click
        # menu on empty timeline space, from TRACK_ADD_ITEMS)
        tracks_menu = menubar.addMenu("Tracks")
        for label, handler in self._track_add_items():
            action = QAction(label, self)
            action.triggered.connect(handler)
            tracks_menu.addAction(action)

        # Player: one top-level menu item, no dropdown (stub for now)
        player_action = QAction("Player", self)
        player_action.triggered.connect(lambda checked=False: self.open_player())
        menubar.addAction(player_action)

        # BUILD 116: Render (one Render Manager per sequence)
        render_action = QAction("Render", self)
        render_action.triggered.connect(lambda checked=False: self.open_render_manager())
        menubar.addAction(render_action)

        # BUILD 166: Refresh Preview (also F5, the QShortcut above).
        # BUILD 178: under a Preview menu, with Clear Video / Clear Audio
        # (mark everything dirty, no refresh; for timing refreshes)
        preview_menu = menubar.addMenu("Preview")
        refresh_action = QAction("Refresh (F5)", self)
        refresh_action.triggered.connect(lambda checked=False: self.refresh_preview())
        preview_menu.addAction(refresh_action)
        clear_video_action = QAction("Clear Video", self)
        clear_video_action.triggered.connect(lambda checked=False: self.clear_preview_video())
        preview_menu.addAction(clear_video_action)
        clear_audio_action = QAction("Clear Audio", self)
        clear_audio_action.triggered.connect(lambda checked=False: self.clear_preview_audio())
        preview_menu.addAction(clear_audio_action)

    def clear_preview_video(self):
        # BUILD 178: every preview frame dirty (0 .. last frame); the next
        # refresh rebuilds them all. Starts no refresh.
        if self.sequence_id is None or not self._project_db():
            return
        try:
            from sequence_preview import FramesMarkDirty
            from sequence_preview_worker import get_worker
            FramesMarkDirty(self._project_db(), self.sequence_id,
                            [(0, max(0, int(self.total_frames()) - 1))])
            get_worker().wake(self.sequence_id)
            self.ruler.viewport().update()
        except Exception as e:
            print(f"Clear preview video failed: {e}")

    def clear_preview_audio(self):
        # BUILD 178: preview audio dirty; the next refresh mixes it in full
        # (no partial mix). Starts no refresh.
        if self.sequence_id is None or not self._project_db():
            return
        try:
            from sequence_preview_audio import clear_preview_audio
            # a running mix is cancelled; the 50 ms poll collects it
            clear_preview_audio(self._project_db(), self.sequence_id)
            self.ruler.viewport().update()
        except Exception as e:
            print(f"Clear preview audio failed: {e}")

    def refresh_preview(self):
        # BUILD 166: explicit refresh. The worker builds every not-clean
        # preview frame (dirty marks kept as before) and the audio mix
        # starts; normal editing only rebuilds the playhead frame
        if self.sequence_id is None:
            return
        # BUILD 169: the audio mix thread starts first, then runs alongside
        # the image pass
        self._audio_auto_failed = False
        self._audio_mix_kick()
        try:
            from sequence_preview_worker import set_refresh_all
            set_refresh_all(True)
        except Exception as e:
            print(f"Refresh preview failed: {e}")
        # BUILD 172: window up (and painted) before the retarget, so it
        # shows even while the refresh pass keeps the GPU busy
        self._show_refresh_dialog()
        self._retarget_preview(force=True)
        self.status_bar.showMessage("Refreshing preview", 3000)

    def _show_refresh_dialog(self):
        # BUILD 167: progress window, closes itself when both are done
        try:
            from refresh_preview_dialog import RefreshPreviewDialog
            if getattr(self, "_refresh_dialog", None) is None:
                self._refresh_dialog = RefreshPreviewDialog(self)
                # BUILD 229: under this timeline's menu bar when created
                # (no geometry memory: every new dialog)
                from window_place import place_under_parent
                place_under_parent(self._refresh_dialog, self, "refresh_preview")
            self._refresh_dialog.start()
            QApplication.processEvents()    # BUILD 172: paint it now
        except Exception as e:
            print(f"Refresh preview window failed: {e}")

    def _project_db(self):
        return _get_global_setting("current_project", "")

    def _caption_find(self):
        from sequence_timeline_find import open_caption_find
        open_caption_find(self)

    def _caption_find_next(self):
        from sequence_timeline_find import caption_find_next
        caption_find_next(self)

    def _caption_find_prev(self):
        from sequence_timeline_find import caption_find_prev
        caption_find_prev(self)

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

    # BUILD 156: keyboard Copy / Cut / Paste (Ctrl+C / X / V)
    def _key_copy(self):
        from sequence_timeline_slice import text_input_focused
        if text_input_focused(self):
            return
        self._clip_copy()

    def _key_cut(self):
        from sequence_timeline_slice import text_input_focused
        if text_input_focused(self):
            return
        self._clip_cut()

    def _key_paste(self):
        from sequence_timeline_slice import text_input_focused
        if text_input_focused(self):
            return
        self._clip_paste_tic()

    def _add_clipboard_actions(self, menu):
        # BUILD 156: Copy / Cut / Paste on a clip's right-click menu. Paste
        # only when this sequence's clipboard has something (hidden, not
        # disabled). Returns (copy, cut, paste or None).
        # BUILD 159: called first on a new menu: Copy, Cut, Paste at the top,
        # then one separator before the rest of the menu
        from timeline_clipboard import has_clipboard
        copy_action = menu.addAction("Copy")
        cut_action = menu.addAction("Cut")
        # paste_action = menu.addAction("Paste") if has_clipboard(self) else None
        # BUILD 160: Paste is a submenu: To Mouse, To Tick; paste_action is
        # then the (to mouse, to tick) pair, or None when hidden
        paste_action = self._add_paste_submenu(menu) if has_clipboard(self) else None
        menu.addSeparator()
        return copy_action, cut_action, paste_action

    def _add_paste_submenu(self, menu):
        # BUILD 160: Paste > To Mouse (the frame under the right-click) /
        # To Tick (the playhead). Returns (to mouse, to tick) actions.
        paste_menu = menu.addMenu("Paste")
        return (paste_menu.addAction("To Mouse"), paste_menu.addAction("To Tick"))

    def _paste_mouse_frame(self, pos):
        # BUILD 160: frame under the right-click, the same x -> frame
        # conversion as drop (whole frames, never below 0); a click left of
        # the Clips column (below-tracks space) counts as its left edge
        return self._frame_at_clips_x(max(0, self._x_in_clips(pos)))

    def _clip_paste_mouse(self, pos):
        from timeline_clipboard import paste
        paste(self, self._paste_mouse_frame(pos), True)

    def _run_paste_pair(self, action, pair, pos):
        # BUILD 160: True when action is To Mouse / To Tick of pair (run it)
        if pair is None or action is None:
            return False
        if action == pair[0]:
            self._clip_paste_mouse(pos)
            return True
        if action == pair[1]:
            self._clip_paste_tic()
            return True
        return False

    def _add_empty_paste_action(self, menu):
        # BUILD 157: Paste on the empty-space / empty track menus, after a
        # separator at the end; None (not added) when the clipboard is empty
        # BUILD 159: called first on a new menu: Paste at the top, then a
        # separator; nothing at all (no separator) when the clipboard is empty
        from timeline_clipboard import has_clipboard
        if not has_clipboard(self):
            return None
        # menu.addSeparator()
        # return menu.addAction("Paste")
        # paste_action = menu.addAction("Paste")
        # BUILD 160: Paste submenu (To Mouse, To Tick); returns the pair
        paste_action = self._add_paste_submenu(menu)
        menu.addSeparator()
        return paste_action

    # def _run_clipboard_action(self, action, acts, clip):
    def _run_clipboard_action(self, action, acts, clip, pos=None):
        # BUILD 156: True when action was one of acts. Copy / Cut use the
        # selection (the right-click already selected an unselected clicked
        # clip with its link partners); with no selection, the clicked clip.
        # Paste: at the playhead (Paste To Tic).
        # BUILD 160: paste_action is the (To Mouse, To Tick) pair; To Mouse
        # pastes at the frame under pos (the right-click)
        copy_action, cut_action, paste_action = acts
        # if action is None or action not in [a for a in acts if a is not None]:
        #     return False
        # if action == paste_action:
        #     self._clip_paste_tic()
        #     return True
        if action is None:
            return False
        if self._run_paste_pair(action, paste_action, pos):
            return True
        if action not in (copy_action, cut_action):
            return False
        if not self._selected_clip_ids:
            self._select_clip(clip, "normal")
        if action == copy_action:
            self._clip_copy()
        else:
            self._clip_cut()
        return True


    def _row_track_id(self, row):
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else None

    # BUILD 161: TRACK_ADD_ITEMS, _track_add_items, _add_track_actions ->
    # sequence_timeline_tracks.py (attached above)

    def _empty_space_menu(self, pos):
        # Right-click below the last track (or in a sequence with no
        # tracks): only the Tracks menu items
        menu = QMenu(self)
        # BUILD 157: Paste (at the playhead) when the clipboard has items
        # BUILD 159: first item
        paste_action = self._add_empty_paste_action(menu)
        acts = self._add_track_actions(menu)
        # paste_action = self._add_empty_paste_action(menu)
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        # if paste_action is not None and action == paste_action:
        #     self._clip_paste_tic()
        #     return
        if self._run_paste_pair(action, paste_action, pos):     # BUILD 160
            return
        if action in acts:
            acts[action]()

    def _exit_app(self):
        # Project Manager is found by class name (it may run as __main__)
        for w in QApplication.topLevelWidgets():
            if type(w).__name__ == "ProjectManager":
                w.close()
                return
        QApplication.quit()

    def _status_bar_menu(self, pos):
        text = self.status_bar.currentMessage()
        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        copy_action.setEnabled(bool(text))
        if menu.exec(self.status_bar.mapToGlobal(pos)) == copy_action:
            QApplication.clipboard().setText(text)

    def _track_context_menu(self, pos):
        # Key boxes first so right-click does not open the clip menu
        from keyframe import hit_test_key, key_context_menu
        key_hit = self._hit_key(pos)
        if key_hit is not None:
            key_context_menu(self, key_hit, pos)
            return
        # Markers next: Edit / Delete instead of the clip menu
        from markers import hit_test_marker, marker_context_menu
        marker_hit = self._hit_marker(pos)
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
            # BUILD 123: empty space below the tracks -> Tracks menu items
            self._empty_space_menu(pos)
            return
        name_item = self.table.item(item.row(), 0)
        if name_item is None:
            return
        track_id = name_item.data(Qt.UserRole)
        if track_id is None:
            return
        enabled = bool(name_item.data(Qt.UserRole + 1))
        # Right-click a track outside the track selection: select just it
        if track_id not in self._selected_track_ids:
            self._selected_track_ids = {track_id}
            self._track_anchor_id = track_id

        menu = QMenu(self)
        # BUILD 157: empty track space: Paste (at the playhead, same as
        # Ctrl+V) when the clipboard has items, hidden when empty
        # BUILD 159: Paste first (then a separator), same empty-clip-area
        # rule as the Tracks items below
        paste_action = None
        if self._x_in_clips(pos) >= 0 and self._clip_edge_at(pos) is None:
            paste_action = self._add_empty_paste_action(menu)
        # BUILD 161: Move > Up / Down / To Top / To Bottom, Disable / Enable,
        # Solo, Track Display Properties: sequence_timeline_tracks.py
        track_acts = self._add_track_menu_items(menu, name_item, enabled)
        menu.addSeparator()
        delete_track_action = menu.addAction("Delete Track")
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
        # BUILD 123: empty clip area of a track row (not a clip body or
        # edge; key boxes / markers / clips returned above) also lists the
        # Tracks menu items. The Name / Type header cells do not.
        add_track_acts = {}
        # paste_action = None
        if x_in_clips >= 0 and self._clip_edge_at(pos) is None:
            menu.addSeparator()
            add_track_acts = self._add_track_actions(menu)
            # BUILD 157: empty track space: Paste (at the playhead, same as
            # Ctrl+V) when the clipboard has items, hidden when empty
            # paste_action = self._add_empty_paste_action(menu)
        # BUILD 163: yellow outline on this track's Name cell while open
        self._menu_track_row = item.row()
        self.table.viewport().update()
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        self._menu_track_row = -1
        self.table.viewport().update()
        # if paste_action is not None and action == paste_action:
        #     self._clip_paste_tic()
        #     return
        if self._run_paste_pair(action, paste_action, pos):     # BUILD 160
            return
        if action in add_track_acts:
            add_track_acts[action]()
            return
        if add_keyframe_action is not None and action == add_keyframe_action:
            click_frame = max(0.0, self.view_start_frame + x_in_clips / self.pixels_per_frame)
            self.add_keyframe_clip(track_id, click_frame)
            return
        if add_caption_action is not None and action == add_caption_action:
            click_frame = max(0.0, self.view_start_frame + x_in_clips / self.pixels_per_frame)
            self.add_caption(track_id, click_frame)
            return
        if ripple_action is not None and action == ripple_action:
            click_frame = max(0.0, self.view_start_frame + x_in_clips / self.pixels_per_frame)
            self.ripple_delete(track_id, click_frame)
            return
        # BUILD 161: Move / Enable / Solo / Track Display Properties in
        # sequence_timeline_tracks._run_track_menu_action (same order)
        if self._run_track_menu_action(action, track_acts, track_id):
            return
        if action == delete_track_action:
            self.delete_track(track_id)

    # BUILD 161: _track_display_properties -> sequence_timeline_tracks.py


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
        pos = event.position().toPoint()
        row = self.table.rowAt(pos.y())
        x_in_clips = pos.x() - self.ruler.sectionViewportPosition(CLIPS_COL)
        # BUILD 105: sequence from the Sequence Manager. Copy action so the
        # manager's row is never removed by a Move drop.
        child_id = sequence_id_from_mime(event.mimeData())
        if child_id is not None:
            event.setDropAction(Qt.CopyAction)
            event.accept()
            self.drop_sequence(child_id, row, x_in_clips)
            return
        footage_id = self._footage_id_from_mime(event.mimeData())
        if footage_id is None:
            event.ignore()
            return
        if self.drop_footage(footage_id, row, x_in_clips):
            event.acceptProposedAction()
        else:
            event.ignore()


    # BUILD 161: _track_row_height -> sequence_timeline_tracks.py

    def _row_collapsed(self, pos):
        # BUILD 126: True if the track row under pos is collapsed
        row = self.table.rowAt(pos.y())
        if row < 0:
            return False
        item = self.table.item(row, 0)
        return bool(item is not None and item.data(ROLE_COLLAPSED))

    def _hit_key(self, pos):
        # Key boxes are not drawn on collapsed tracks: not hittable there
        if self._row_collapsed(pos):
            return None
        from keyframe import hit_test_key
        return hit_test_key(self, pos)

    def _hit_marker(self, pos):
        # Marker squares are not drawn on collapsed tracks: not hittable
        if self._row_collapsed(pos):
            return None
        from markers import hit_test_marker
        return hit_test_marker(self, pos)

    # BUILD 161: _toggle_collapse_at -> sequence_timeline_tracks.py

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
        # this size is used.
        # Misses are remembered in _icon_miss (key -> True if generation was
        # tried, False if only storage was read), so repaints do not query
        # the project DB or retry a failed grab for every clip. A storage-only
        # miss does not block a later generate call. The misses are dropped
        # whenever an icon is saved in this process (footage_manager_display
        # ICON_SAVE_SERIAL), on timeline refresh, and with _icon_cache.
        frame_number = max(0, int(frame_number or 0))
        key = (footage_id, frame_number, height)
        if key in self._icon_cache:
            return self._icon_cache[key]
        import footage_manager_display
        serial = footage_manager_display.ICON_SAVE_SERIAL
        if serial != self._icon_miss_serial:
            self._icon_miss.clear()
            self._icon_miss_serial = serial
        tried = self._icon_miss.get(key)
        if tried is not None and (tried or not generate):
            return None
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
        except Exception:
            pix = None
        if pix is None or pix.isNull():
            # A grab saves icons even when this exact key is not found;
            # take the new serial so the miss below is not dropped at once
            self._icon_miss_serial = footage_manager_display.ICON_SAVE_SERIAL
            self._icon_miss[key] = bool(generate)
            return None
        self._icon_cache[key] = pix
        self._icon_miss.pop(key, None)
        return pix

    def _clear_icon_misses(self):
        # Forget remembered icon misses so the next paint looks again
        self._icon_miss.clear()

    def go_home(self):
        # Fully left: frame 0 after the left spacer
        self.view_start_frame = view_min_start(self.pixels_per_frame)
        self.current_frame = 0
        self._clamp_view()
        self.ruler.viewport().update()
        self.table.viewport().update()
        self._update_status()
        self._save_view_state()

    def go_end(self):
        # last frame in view with the right padding after it
        self.view_start_frame = view_max_start(
            self.total_frames(), self.visible_frames(), self.pixels_per_frame)
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
        # may scroll TIMELINE_RIGHT_PAD_PX past the last frame (view_max_start)
        # and TIMELINE_LEFT_PAD_PX before frame 0 (view_min_start)
        max_start = view_max_start(self.total_frames(), self.visible_frames(),
                                   self.pixels_per_frame)
        self.view_start_frame = max(view_min_start(self.pixels_per_frame),
                                    min(max_start, int(self.view_start_frame)))

    def _update_clips_width(self):
        # Clips width is set by the Stretch mode; just keep the view valid and repaint.
        # A view inside the left spacer (zoom or resize) stays fully left at the
        # new zoom.
        if self.view_start_frame < 0:
            self.view_start_frame = view_min_start(self.pixels_per_frame)
        self._clamp_view()
        self.ruler.viewport().update()
        self.table.viewport().update()

    def wheel_pan(self, event):
        # Wheel up pans earlier, wheel down pans later. About one second per notch.
        dy = event.angleDelta().y()
        alt = bool(event.modifiers() & Qt.AltModifier)
        if alt and dy == 0:
            # Qt may turn Alt+wheel into a horizontal wheel; use that delta
            dy = event.angleDelta().x()
        if dy == 0:
            event.ignore()
            return
        if alt:
            # Alt+wheel zooms: up = zoom in, down = zoom out, keeping the
            # frame under the mouse in place
            x_in_clips = event.position().x() - self.ruler.sectionViewportPosition(CLIPS_COL)
            anchor = self.view_start_frame + max(0.0, x_in_clips) / self.pixels_per_frame
            self._zoom_by(WHEEL_ZOOM_STEP if dy > 0 else 1.0 / WHEEL_ZOOM_STEP)
            if x_in_clips >= 0:
                self.view_start_frame = int(round(
                    anchor - x_in_clips / self.pixels_per_frame))
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

    def _slice_enter(self):
        from sequence_timeline_slice import enter_slice_mode
        enter_slice_mode(self)

    def _slice_exit_key(self):
        from sequence_timeline_slice import slice_key_exit
        slice_key_exit(self)

    def keyPressEvent(self, event):
        # BUILD 131: Escape leaves slice mode (keys a child did not take)
        if (event.key() == Qt.Key_Escape and event.modifiers() == Qt.NoModifier
                and self._slice_mode):
            from sequence_timeline_slice import slice_key_exit
            if slice_key_exit(self):
                event.accept()
                return
        super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.table.viewport():
            etype = event.type()
            if (getattr(self, "_slice_mode", False)
                    or getattr(self, "_slice_eat_release", False)):
                # BUILD 131: slice mode takes the mouse first (BUILD 269:
                # and the release of the click that cut and left it)
                from sequence_timeline_slice import slice_event
                handled = slice_event(self, obj, event)
                if handled is not None:
                    return handled
            # BUILD 148 / 149: left-drag rubber-band selection in empty
            # track space (a plain left-click is unchanged). BUILD 150:
            # link partners, edge auto-scroll, live selection borders
            from sequence_timeline_rubber import rubber_event
            handled = rubber_event(self, obj, event)
            if handled is not None:
                return handled
            if etype == QEvent.Resize and getattr(self, "playhead_overlay", None):
                self.playhead_overlay.setGeometry(obj.rect())
                self.playhead_overlay.raise_()
            if etype == QEvent.Wheel:
                self.wheel_pan(event)
                return True
            if etype in (QEvent.DragEnter, QEvent.DragMove):
                # BUILD 125: footage drop preview (sequence drops: none)
                from sequence_timeline_droppreview import drag_enter, drag_move, drag_end
                if sequence_id_from_mime(event.mimeData()) is not None:
                    drag_end(self)
                    event.setDropAction(Qt.CopyAction)
                    event.accept()
                else:
                    fid = self._footage_id_from_mime(event.mimeData())
                    if fid is not None:
                        event.acceptProposedAction()
                        try:
                            pos = event.position().toPoint()
                            if etype == QEvent.DragEnter or getattr(self, "_drop_info", None) is None:
                                drag_enter(self, fid, pos)
                            else:
                                drag_move(self, pos)
                        except Exception:
                            drag_end(self)
                    else:
                        drag_end(self)
                        event.ignore()
                return True
            if etype == QEvent.DragLeave:
                # BUILD 125: leave / Esc cancel: no stale preview box
                from sequence_timeline_droppreview import drag_end
                drag_end(self)
                return False
            if etype == QEvent.Drop:
                from sequence_timeline_droppreview import drag_end
                drag_end(self)
                try:
                    self._handle_drop(event)
                finally:
                    drag_end(self)
                return True
            if etype == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                # Double-click a key box: edit value only (no clip actions)
                from keyframe import hit_test_key, edit_key
                dbl_pos = event.position().toPoint()
                key_hit = self._hit_key(dbl_pos)
                if key_hit is not None:
                    edit_key(self, key_hit["clip"], key_hit["key_id"],
                             key_hit["frame"], key_hit["value"])
                    return True
                # BUILD 236: open caption clip content pane: in-place text
                # editor (title bar double-click: unchanged, nothing)
                from sequence_timeline_captions import caption_pane_at, edit_caption_inline
                pane_hit = caption_pane_at(self, dbl_pos)
                if pane_hit is not None:
                    edit_caption_inline(self, pane_hit[0], pane_hit[1])
                    return True
                return False
            if etype == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                # Click selection: normal = clip + link partners, Alt = only
                # that clip (selection solo), Shift = add to the selection,
                # empty clip-area space = clear
                press_pos = event.position().toPoint()
                # BUILD 126: triangle cell toggles collapsed (nothing else)
                if self._toggle_collapse_at(press_pos):
                    return True
                # Name column: track selection, then the usual press handling
                self._track_name_click(event, press_pos)
                # Marker under the mouse: marker drag only (no clip select /
                # drag, no edit popup)
                from markers import hit_test_marker, marker_drag_start
                marker_hit = self._hit_marker(press_pos)
                if marker_hit is not None:
                    marker_drag_start(self, marker_hit, press_pos)
                    return True
                # BUILD 162: clip click selection -> sequence_timeline_clips
                hit_clip = self._clip_press_select(event, press_pos)
                from keyframe import hit_test_key
                key_hit = self._hit_key(press_pos)
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
                # BUILD 162: clip edge trim / move drag start -> sequence_timeline_clips
                if self._clip_press_drag(event):
                    return True
                # BUILD 150: a left-click on empty track space (Clips area)
                # is not passed on to the table, so the track row is not
                # selected / highlighted (it keeps its color); the clip
                # selection was cleared above as before
                if hit_clip is None and self._x_in_clips(press_pos) >= 0:
                    return True
            if etype == QEvent.MouseMove:
                pos = event.position().toPoint()
                if getattr(self, "_marker_drag", None) is not None:
                    from markers import marker_drag_move
                    marker_drag_move(self, pos)
                    return True
                if getattr(self, "_key_drag", None) is not None:
                    from keyframe import key_drag_move
                    key_drag_move(self, pos)
                    return True
                if getattr(self, "_edge_drag", None) is not None:
                    self._edge_drag_move(pos)
                    return True
                self._clip_edge_cursor(obj, pos)     # BUILD 162
                # Hovered marker: 3 px line; repaint only when it changes
                from markers import hit_test_marker
                mh = self._hit_marker(pos)
                self._set_hover_marker(mh[1][0] if mh is not None else None)
            if etype == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if getattr(self, "_marker_drag", None) is not None:
                    from markers import marker_drag_release
                    marker_drag_release(self)
                    return True
                if getattr(self, "_key_drag", None) is not None:
                    from keyframe import key_drag_release
                    key_drag_release(self)
                    return True
                if getattr(self, "_edge_drag", None) is not None:
                    self._clip_drag_release()     # BUILD 162
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

    def _track_type(self, track_id):
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item is not None and item.data(Qt.UserRole) == track_id:
                return (item.data(Qt.UserRole + 4) or "").lower()
        return ""

    def _mark_preview_dirty(self, ranges):
        # Clean set drops these frames now (bar turns red); the database
        # UPDATE runs on the preview worker thread. refresh() wakes the worker.
        # BUILD 146: per-edit marks are gone: every completed edit reaches
        # refresh(), whose mile-marker diff marks exactly the spans that
        # differ (callers kept; this does nothing).
        return
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

    def _add_link_actions(self, menu, clip):
        # BUILD 133: one of Link / Unlink (sequence_timeline_link.
        # link_menu_choice): all selected in one group -> Unlink; any
        # unlinked or a second group in the selection -> Link; a one-member
        # (dangling) link counts as unlinked. Returns (link, unlink) actions.
        from sequence_timeline_link import link_menu_choice
        choice, enabled = link_menu_choice(self._selected_clips(), clip,
                                           self.clips_by_track)
        if choice == "unlink":
            return None, menu.addAction("Unlink")
        link_action = menu.addAction("Link")
        link_action.setEnabled(enabled)
        return link_action, None

    def link_selected_clips(self, clip=None):
        # Link: every selected clip joins one link_id (footage clips /
        # captions / keyframe_clips each store their own). BUILD 133: the
        # winner is the right-clicked clip's group, else the first selected
        # clip's group, else the first selected clip's id; every member of
        # each group the selection touches joins (groups merge).
        from sequence_timeline_link import link_plan
        plan = link_plan(self._selected_clips(), clip, self.clips_by_track)
        if plan is None:
            return
        new_link, selected = plan
        if not selected:
            return
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

    def _zoom_by(self, factor):
        # Multiply the zoom by factor, within ZOOM_MIN_PPF..ZOOM_MAX_PPF
        ppf = clamp_zoom(self.pixels_per_frame * factor)
        if ppf != self.pixels_per_frame:
            self.pixels_per_frame = ppf
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
            self.playhead_overlay.update_playhead()
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
        # Table and clips reload (sequence_timeline_sql.py), then the player.
        # Icon misses and the caption partner names are looked up again
        # after a reload.
        self._clear_icon_misses()
        self._caption_partner_cache = None
        refresh(self)
        from markers import load_markers
        self._markers = load_markers(self._project_db())
        # Every clip / track change and undo-redo reload comes through here:
        # keep the Sequence Manager counts current (no timer)
        self._refresh_sequence_manager()
        # BUILD 153: no player request here: it ran before the diff marked
        # anything, so it re-read the old clean JPEG. The retarget below
        # redraws the shown frame when a dirty span covers it.
        # self._update_player()
        # BUILD 135: completed edit / undo / reload: the audio mix may be
        # stale (the scrub burst rechecks the signature; Play always does).
        # Drags never get here before the drop.
        # BUILD 146: the mile markers of the new state, built once here and
        # used by both the audio bar ranges and the video dirty ranges
        markers = self._build_mile_markers()
        try:
            from sequence_preview_audio import (mark_preview_audio_dirty,
                                                audio_edit_ranges)
            if self.sequence_id is not None and self._project_db():
                mark_preview_audio_dirty(self._project_db(), self.sequence_id)
                # BUILD 140: the bar shows the edit's dirty ranges at once
                # (only what the next mix remixes; a pure move copies
                # samples and stays green), not all red until the settle
                audio_edit_ranges(self._project_db(), self.sequence_id,
                                  float(self.fps or 30), int(self.total_frames()),
                                  markers=markers)
                # BUILD 137: audio bar red now; settle the true state with
                # one signature check AUDIO_SETTLE_MS after the last refresh.
                # BUILD 138: that check also starts the background mix.
                # A new edit retries a failed mix.
                self._audio_auto_failed = False
                self._audio_bar_schedule()
        except Exception:
            pass
        # Edit: new snapshot, wake the preview worker
        self._retarget_preview(force=True, markers=markers)

    def _footage_paths(self):
        # BUILD 146: footage id -> original path (the video span descriptor:
        # a relink or undo changes the picture under the same footage id)
        try:
            conn = sqlite3.connect(self._project_db())
            try:
                rows = conn.execute("SELECT id, original_path FROM footage").fetchall()
            finally:
                conn.close()
            return dict((int(r[0]), r[1]) for r in rows)
        except Exception:
            return {}

    def _video_index(self, snap):
        from mile_markers import VideoIndex
        try:
            size = tuple(self._sequence_size())
        except Exception:
            size = None
        return VideoIndex(snap, self._footage_paths(), size)

    def _build_mile_markers(self, snap=None):
        # BUILD 146: the mile markers (mile_markers.MarkerList) of the
        # timeline now: the preview snapshot's layer stacks and the audio
        # mix inputs. None on any failure (the caller falls back).
        if self.sequence_id is None or not hasattr(self, "table"):
            return None
        try:
            from sequence_player import timeline_snapshot
            from mile_markers import MarkerList, AudioIndex
            if snap is None:
                snap = timeline_snapshot(self)
            video = self._video_index(snap)
            audio = inputs = layout = None
            try:
                from sequence_preview_audio import _mix_inputs, _layout_of
                db = self._project_db()
                inputs = _mix_inputs(db, self.sequence_id)
                layout = _layout_of(inputs, float(self.fps or 30),
                                    int(self.total_frames()), "")
                audio = AudioIndex(layout)
            except Exception as e:
                print(f"Mile markers (audio) failed: {e}")
                audio = inputs = layout = None
            ml = MarkerList(video, audio, inputs, layout)
            ml.snap = snap
            return ml
        except Exception as e:
            print(f"Mile markers failed: {e}")
            return None

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
            self.view_start_frame = frame - visible // 2
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

    def _set_control_active(self, name, on):
        # BUILD 163: dark green background while the control's action runs
        btn = getattr(self, "_control_buttons", {}).get(name)
        if btn is not None:
            btn.setStyleSheet("background-color: rgb(0, 125, 0);" if on else "")

    def _toggle_play(self):
        # Same as the Pause button while playing or filling, else Play
        if self._playing or self._play_filling or self._play_mixing:
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
        self._set_control_active("Play", True)    # BUILD 163: until _stop_play
        try:
            from sequence_player import open_player_for
            open_player_for(self)
        except Exception as e:
            print(f"Play could not open the player: {e}")
        self._play_from = int(self.current_frame)
        # BUILD 136: a stale audio mix starts mixing in the background now,
        # alongside any video fill; the clock waits for it (_start_clock)
        mixing = self._audio_mix_kick()
        # BUILD 168: a dirty preview (any not-clean frame in the sequence)
        # runs the F5 refresh first, in the Refreshing Preview window; the
        # clock starts once it is done (_play_ready waits for the refresh)
        dirty = not self._preview_all_clean()
        if not dirty and self._play_ready():
            if mixing:
                self._show_refresh_dialog()
            self._start_clock()
            return
        self._play_filling = True
        try:
            from sequence_preview_worker import set_play_priority, set_refresh_all
            if dirty:
                set_refresh_all(True)
            set_play_priority(True)
        except Exception as e:
            print(f"Play priority failed: {e}")
        if dirty or mixing:
            self._show_refresh_dialog()
        self._retarget_preview(force=True)
        self._play_fill_status()

    def _preview_all_clean(self):
        # BUILD 168: True when every frame of the sequence is a clean preview
        try:
            from sequence_preview import clean_set
            clean = clean_set(self._project_db(), self.sequence_id)
        except Exception:
            return True
        return all(f in clean for f in range(self.total_frames()))

    def _play_ready(self):
        # True when every frame from the tick to the end is a clean preview
        # (BUILD 168: and an explicit refresh pass, if any, has finished)
        try:
            from sequence_preview_worker import _refresh_all
            if _refresh_all[0]:
                return False
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

    def _audio_mix_kick(self):
        # BUILD 136: start (or reuse) the background mix when the WAV does
        # not match the timeline. True when a mix is running for this Play.
        try:
            from sequence_preview_audio import (preview_audio_current, start_mix,
                                                cancel_mix)
            db = self._project_db()
            if not db or self.sequence_id is None:
                return False
            fps = float(self.fps or 30)
            total = int(self.total_frames())
            wav, sig = preview_audio_current(db, self.sequence_id, fps, total)
            self.ruler.viewport().update()     # BUILD 137: Play's check
            if wav or not sig:
                # Up to date: a mix of some other (older) state is useless
                cancel_mix(db, self.sequence_id)
                self._audio_mix_job = None
                return False
            self._audio_mix_job = start_mix(db, self.sequence_id, fps, total, sig)
            if self._audio_mix_timer is None:
                self._audio_mix_timer = QTimer(self)
                self._audio_mix_timer.setInterval(50)
                self._audio_mix_timer.timeout.connect(self._audio_mix_poll)
            self._audio_mix_timer.start()
            self._preview_status_poll()
            return True
        except Exception as e:
            print(f"Audio mix start failed: {e}")
            self._audio_mix_job = None
            return False

    def _audio_bar_schedule(self):
        # BUILD 137: debounce (restart) the audio state check
        if self._audio_bar_timer is None:
            self._audio_bar_timer = QTimer(self)
            self._audio_bar_timer.setSingleShot(True)
            self._audio_bar_timer.setInterval(AUDIO_SETTLE_MS)
            self._audio_bar_timer.timeout.connect(self._audio_bar_check)
        self._audio_bar_timer.start()
        try:
            self.ruler.viewport().update()
        except Exception:
            pass

    def _audio_bar_check(self):
        # BUILD 137: one signature check on the GUI thread (a few small
        # queries); green when the WAV is current and no mix runs.
        # BUILD 138: a stale / missing WAV starts the background mix here
        # (no Play needed); a running mix of an older state is restarted
        # (start_mix reuses a job that already mixes the current state).
        try:
            if (getattr(self, "_edge_drag", None) is not None
                    or getattr(self, "_key_drag", None) is not None
                    or getattr(self, "_marker_drag", None) is not None):
                self._audio_bar_schedule()      # never mid-drag: try later
                return
            from sequence_preview_audio import (settle_audio_state, running_mix,
                                                any_mix_running)
            db = self._project_db()
            if db and self.sequence_id is not None:
                if running_mix(db, self.sequence_id) is not None:
                    # BUILD 139: settle the bar's dirty ranges, then restart
                    # / keep the running mix
                    settle_audio_state(db, self.sequence_id, float(self.fps or 30),
                                       int(self.total_frames()))
                    # BUILD 166: no restart here (explicit refresh model:
                    # F5 or Play mixes)
                    # self._audio_mix_kick()
                elif not settle_audio_state(db, self.sequence_id,
                                            float(self.fps or 30),
                                            int(self.total_frames())):
                    if self._audio_auto_failed:
                        pass        # failed: wait for an edit or Play
                    elif any_mix_running(exclude=(db, self.sequence_id)):
                        # global cap: one auto mix at a time (the poll retries)
                        self._audio_auto_wanted = True
                    else:
                        self._audio_auto_wanted = False
                        # BUILD 166: no background mix while editing; F5 or
                        # Play starts it
                        # self._audio_mix_kick()
        except Exception as e:
            print(f"Audio state check failed: {e}")
        try:
            self.ruler.viewport().update()
        except Exception:
            pass

    def _audio_mix_finish(self, job, pending):
        # Finished job: collect it (GUI thread: reload generation). A stale
        # or superseded result while Play still waits starts a fresh mix.
        from sequence_preview_audio import collect_mix
        status = collect_mix(job)
        if self._audio_mix_job is job:
            self._audio_mix_job = None
        if status in ("stale", "cancelled") and pending:
            self._audio_mix_kick()
        elif status == "failed":
            # BUILD 138: red, and no automatic retry loop
            self._audio_auto_failed = True
        elif status == "stale":
            # BUILD 138: an edit landed during the auto mix: settle again
            self._audio_bar_schedule()
        return status

    def _audio_mix_wait(self):
        # _start_clock gate: True = Play waits for the running mix
        job = getattr(self, "_audio_mix_job", None)
        if job is None:
            return False
        if job.done:
            self._audio_mix_finish(job, True)
            job = self._audio_mix_job
            if job is None or job.done:
                return False
        self._play_filling = False
        self._play_mixing = True
        self._preview_status_poll()
        return True

    def _audio_mix_poll(self):
        # 50 ms GUI-thread poll while a mix runs
        job = getattr(self, "_audio_mix_job", None)
        if job is None:
            if self._audio_mix_timer is not None:
                self._audio_mix_timer.stop()
            self._preview_status_poll()
            return
        if not job.done:
            return
        self._audio_mix_finish(job, self._play_mixing or self._play_filling)
        if self._audio_mix_job is not None:
            return      # a fresh mix runs
        if self._audio_mix_timer is not None:
            self._audio_mix_timer.stop()
        self._preview_status_poll()
        # BUILD 137: collect_mix settled the audio bar
        self.ruler.viewport().update()
        if self._play_mixing:
            self._play_mixing = False
            self._start_clock()

    def _start_clock(self):
        # Prime the start-frame batch, then audio, then the tick timer.
        # Do not start the WAV until play_has(start) is true.
        # BUILD 136: the whole Play (video too) waits for a running audio
        # mix so A/V start together with the new WAV
        if self._audio_mix_wait():
            return
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
        self._play_resync_t = None      # BUILD 244: first resync 1 s after the first frame
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

        # BUILD 244: periodic resync (see RESYNC_INTERVAL_S). _play_cursor
        # is the next frame to show; behind = it is before the due frame
        if self._play_audio_clock:
            now = time.perf_counter()
            last_sync = getattr(self, "_play_resync_t", None)
            if last_sync is None:
                self._play_resync_t = now
            elif now - last_sync >= RESYNC_INTERVAL_S:
                self._play_resync_t = now
                if self._play_cursor < due:
                    try:
                        player.play_drop_before(due)
                    except Exception:
                        pass
                    self._play_cursor = due

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

        # BUILD 244: per-tick catch-up. When the next frame is already
        # behind the due frame and the due frame is decoded, jump to it
        # (older queued frames are dropped) instead of showing the late
        # frames one per tick, which never closes the gap
        if self._play_audio_clock and self._play_cursor < due:
            try:
                if player.play_has(due):
                    player.play_drop_before(due)
                    self._play_cursor = due
            except Exception:
                pass

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
            self.view_start_frame = self._play_cursor - visible // 4
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
        # BUILD 136: Stop drops the pending start; a running mix finishes and
        # is kept when its signature is still current
        self._play_mixing = False
        self._playing = False
        self._play_filling = False
        self._play_prime_tries = 0
        self._play_timer.stop()
        self._set_control_active("Play", False)   # BUILD 163
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

    def open_render_manager(self):
        # Render menu: this sequence's Render Manager, raised if open
        try:
            from render_manager import open_render_manager
            open_render_manager(self)
        except Exception as e:
            QMessageBox.warning(self, "Render", f"Could not open the Render Manager: {e}")

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
            f"View from {max(0, self.view_start_frame)}  "
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
            f"View from {max(0, self.view_start_frame)}  "
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

    def _retarget_preview(self, force=False, markers=None):
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
        cap_redraw = False      # BUILD 153: caption change seen
        try:
            from sequence_preview_worker import get_worker
            from sequence_player import timeline_snapshot
            # Snapshot only on edit, activation or first use; a scrub reuses it
            snap = getattr(self, "_preview_snapshot", None)
            old_snap = snap
            old_ml = getattr(self, "_preview_markers", None)
            new_ml = None
            if force or snap is None:
                # BUILD 146: the refresh's mile markers carry the snapshot
                if force and markers is not None and getattr(markers, "snap", None) is not None:
                    snap = markers.snap
                    new_ml = markers
                else:
                    snap = timeline_snapshot(self)
                self._preview_snapshot = snap
                # BUILD 102: player caption overlay data, same refresh
                old_caps = getattr(self, "_caption_snapshot", None)
                try:
                    from player_captions import caption_snapshot
                    self._caption_snapshot = caption_snapshot(self)
                except Exception:
                    self._caption_snapshot = None
                if (self._caption_snapshot or []) != (old_caps or []):
                    # Redraw the shown frame so a caption edit appears now
                    # BUILD 153: after the dirty marks (below), once
                    # self._update_player()
                    cap_redraw = True
            get_worker().retarget(self._project_db(), self.sequence_id, frame,
                                  self.total_frames(), snap,
                                  getattr(self, "fps", None))
            self._preview_center = frame
            # BUILD 139: the frames this edit changes, from the snapshot
            # before vs after (same data the worker composites from): only
            # those go dirty. A failure marks everything (safe).
            # BUILD 146: mile markers: the new marker list diffed against
            # the previous one (mile_markers.diff_video): differing spans
            # are dirty, a pure move found in the spans relocates its clean
            # JPEG rows (FramesRelocate, queued before the dirty marks);
            # no edit type is looked at.
            diff = []
            relocs = []
            if snap is not old_snap:
                try:
                    if new_ml is None or new_ml.video is None:
                        from mile_markers import MarkerList
                        new_ml = MarkerList(self._video_index(snap))
                        new_ml.snap = snap
                except Exception as e:
                    print(f"Mile markers failed: {e}")
                    new_ml = None
            if force and old_snap is not None and old_snap is not snap:
                try:
                    from mile_markers import diff_video
                    old_v = old_ml.video if (old_ml is not None
                                             and getattr(old_ml, "snap", None) is old_snap) else None
                    if old_v is None:
                        # no list kept for the old snapshot: rebuild it with
                        # the current paths / size (same descriptor format)
                        from mile_markers import VideoIndex
                        old_v = VideoIndex(old_snap, new_ml.video.paths, new_ml.video.size)
                    diff, relocs = diff_video(old_v, new_ml.video)
                except Exception as e:
                    print(f"Preview diff failed: {e}")
                    from sequence_preview_diff import WHOLE
                    diff, relocs = [WHOLE], []
            if new_ml is not None:
                self._preview_markers = new_ml
            if relocs:
                try:
                    from sequence_preview import FramesRelocate
                    FramesRelocate(self._project_db(), self.sequence_id, relocs)
                except Exception as e:
                    # print(f"Preview relocate failed: {e}")
                    from sequence_preview_diff import _merge
                    diff = _merge(list(diff) + [(a + d, b + d) for a, b, d in relocs])
            # BUILD 146: no per-edit shift plan any more (the marker diff
            # finds pure moves); a stale one from an older path is dropped
            pending = getattr(self, "_preview_pending_shift", None)
            self._preview_pending_shift = None
            pending = None
            if force and pending:
                # After the retarget, so the worker's old-snapshot pass
                # cannot start a new chunk; a chunk already rendering was
                # numbered before this shift and stays dirty
                self._preview_pending_shift = None
                try:
                    from sequence_preview import FramesShift
                    from sequence_preview_diff import shift_ok, subtract
                    first, last, delta = pending[0]
                    # BUILD 139: shift only when the snapshots prove every
                    # moved frame is the same picture at its new frame
                    if old_snap is None or not shift_ok(old_snap, snap,
                                                        first, last, delta):
                        raise ValueError("not a pure move")
                    # The shifted spans are FramesShift's (a dirty mark
                    # queued before it would empty the rows it moves)
                    diff = subtract(diff, [(first, last),
                                           (first + delta, last + delta)])
                    FramesShift(self._project_db(), self.sequence_id,
                                first, last, delta)
                except Exception as e:
                    # print(f"Preview shift failed: {e}")
                    self._mark_preview_dirty(pending[1])
            if diff:
                from sequence_preview import FramesMarkDirty
                FramesMarkDirty(self._project_db(), self.sequence_id, diff)
            remark = getattr(self, "_preview_remark", None)
            if force and remark:
                self._preview_remark = []
                from sequence_preview import FramesMarkDirty
                FramesMarkDirty(self._project_db(), self.sequence_id, remark)
            if force and (diff or pending or remark or relocs):
                # BUILD 141: the marks came after the retarget: wake the
                # worker so it sees them (no player needed to retrigger it)
                get_worker().wake(self.sequence_id)
            # BUILD 153: one hook after the diff: a dirty span (marker diff,
            # remarks, relocated targets) covering the tick redraws the
            # player now with the new composite. The worker was centered on
            # the tick above, so its JPEG is rebuilt first.
            if force:
                spans = list(diff) + list(remark or [])
                spans += [(a + d, b + d) for a, b, d in relocs]
                if any(int(a) <= frame <= int(b) for a, b in spans):
                    cap_redraw = False
                    self._redraw_tick(frame, snap)
        except Exception as e:
            print(f"Preview worker retarget failed: {e}")
        if cap_redraw:
            self._update_player()

    def _redraw_tick(self, frame, snap):
        # BUILD 153: the shown frame changed: compose it in the open player
        # now (one frame, the GPU composite path). No player: nothing to
        # draw; the worker already starts at the tick.
        player = getattr(self, "_player", None)
        if player is None or getattr(player, "closed", False):
            return
        try:
            player.redraw_edited(frame, snap)
        except Exception as e:
            print(f"Player tick redraw failed: {e}")

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

    def _preview_status_poll(self):
        # BUILD 129: worker progress -> status label (text set only on change)
        try:
            from sequence_preview_worker import _worker, status_text
            prog = getattr(_worker, "progress", None)
            # The project path (a settings read) only while generating here
            if prog is None or prog[1] != self.sequence_id:
                text = ""
            else:
                text = status_text(_worker, self._project_db(), self.sequence_id)
        except Exception:
            text = ""
        # BUILD 137: a source re-import (the _AudioTmp.db changed; one
        # os.stat per poll) turns the audio bar red, then a settle check
        try:
            from sequence_preview_audio import check_audio_source
            db = self._project_db() if self.sequence_id is not None else ""
            if db and check_audio_source(db, self.sequence_id):
                self._audio_auto_failed = False     # new source: retry
                self._audio_bar_schedule()
            elif db and self._audio_auto_wanted:
                # BUILD 138: an auto mix waits for another sequence's mix
                from sequence_preview_audio import any_mix_running
                if not any_mix_running(exclude=(db, self.sequence_id)):
                    self._audio_auto_wanted = False
                    self._audio_bar_check()
        except Exception:
            pass
        # BUILD 136: both statuses when both run
        job = getattr(self, "_audio_mix_job", None)
        if job is not None and not job.done:
            text = (text + "   " if text else "") + "Mixing audio"
        if getattr(self, "_preview_status", None) is None:
            return
        if self._preview_status.text() != text:
            self._preview_status.setText(text)

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
        # 0 or less is fully left: frame 0 after the left spacer
        view = min(total - 1, read_int("view", 0))
        self.view_start_frame = view if view > 0 else view_min_start(self.pixels_per_frame)

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
        # Preview bar from the existing {ProjectName}_{id}_Preview.db: paint once on open
        if hasattr(self, "ruler"):
            self.ruler.viewport().update()

    def closeEvent(self, event):
        # BUILD 131: slice mode ends with the window
        try:
            from sequence_timeline_slice import exit_slice_mode
            exit_slice_mode(self)
        except Exception:
            pass
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
        if hasattr(self, "_preview_status_timer"):
            self._preview_status_timer.stop()
        # BUILD 136: no poll after close; the worker (no Qt) is cancelled
        # and exits on its own (daemon thread, nothing joined here)
        self._play_mixing = False
        if getattr(self, "_audio_mix_timer", None) is not None:
            self._audio_mix_timer.stop()
        if getattr(self, "_audio_bar_timer", None) is not None:
            self._audio_bar_timer.stop()
        job = getattr(self, "_audio_mix_job", None)
        if job is not None and not job.done:
            job.cancel.set()
        self._audio_mix_job = None
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

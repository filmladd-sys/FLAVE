# sequence_timeline_tracks.py
# BUILD 161: track parts of the Sequence Timeline window, split out of
# sequence_timeline.py with no behavior change: the track table style (no
# track highlight), the collapse triangle / column / toggle, the track
# right-click items (Move, Enable / Disable, Solo, Track Display
# Properties) and the add-track items, the Track Display Properties
# dialog, and the track row (icon) height. Functions take the
# SequenceTimeline as self; sequence_timeline.py imports them and attaches
# them to the window class (same pattern as sequence_timeline_sql.py).
# Shared constants stay in sequence_timeline_sql.py. ASCII only.

import sqlite3
from PySide6.QtWidgets import (
    QApplication, QMessageBox, QHeaderView, QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QCheckBox, QLineEdit, QLabel, QColorDialog, QDialogButtonBox,
    QProxyStyle, QStyle, QStyleFactory, QStyledItemDelegate
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from sequence_timeline_sql import (
    DEFAULT_TRACK_HEIGHT, COLLAPSE_COL, COLLAPSE_COL_WIDTH, COLLAPSED_ROW_HEIGHT,
    ROLE_COLLAPSED, ROLE_OPEN_HEIGHT,
)


# BUILD 158: no track highlight. The track table draws every cell with a
# row panel (QTableView drawCell -> PE_PanelItemViewRow) and the item panel
# (CE_ItemViewItem -> PE_PanelItemViewItem) carrying the Selected /
# MouseOver / HasFocus states; the platform style paints those light grey /
# lighter on hover (header cells and the clip-area lane). This style drops
# those states for the track table only, so a row always paints exactly as
# an unselected, unhovered row; the focus rectangle is not drawn.
_TRACK_STRIP_STATES = (QStyle.State_Selected | QStyle.State_MouseOver
                       | QStyle.State_HasFocus)


def _plain_option(opt):
    opt = type(opt)(opt)
    opt.state = opt.state & ~_TRACK_STRIP_STATES
    return opt


class _NoTrackHighlightStyle(QProxyStyle):
    def drawPrimitive(self, element, option, painter, widget=None):
        if element in (QStyle.PE_PanelItemViewRow, QStyle.PE_PanelItemViewItem):
            option = _plain_option(option)
        elif element == QStyle.PE_FrameFocusRect:
            return
        super().drawPrimitive(element, option, painter, widget)

    def drawControl(self, element, option, painter, widget=None):
        if element == QStyle.CE_ItemViewItem:
            option = _plain_option(option)
        super().drawControl(element, option, painter, widget)


def _track_table_style(table):
    # Same base as the application style (the app never sets one: the
    # platform default), parented to the table so it lives as long
    base = QStyleFactory.create(QApplication.style().name() or "")
    style = _NoTrackHighlightStyle(base) if base is not None else _NoTrackHighlightStyle()
    style.setParent(table)
    return style


# BUILD 163: yellow outline around the Name cell of the track whose
# right-click menu is open, so the user sees which track the menu acts on.
# Same yellow as the selected clip outline (sequence_timeline_clips.py).
MENU_TRACK_OUTLINE = QColor(255, 255, 160)


class NameCellDelegate(QStyledItemDelegate):
    # Name column (0): default paint, plus the outline on tl._menu_track_row
    def __init__(self, timeline):
        super().__init__(timeline.table)
        self.timeline = timeline

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index.row() != getattr(self.timeline, "_menu_track_row", -1):
            return
        painter.save()
        painter.setPen(MENU_TRACK_OUTLINE)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(option.rect.adjusted(0, 0, -1, -1))
        painter.restore()


# ---------------------------------------------------------------- collapse

def paint_collapse_triangle(painter, rect, collapsed):
    # BUILD 126: small filled triangle centered in the cell
    from PySide6.QtGui import QPolygon
    from PySide6.QtCore import QPoint
    s = max(4, min(10, rect.height() - 6, rect.width() - 6))
    cx, cy = rect.center().x(), rect.center().y()
    h = s // 2
    if collapsed:
        pts = [QPoint(cx - h // 2, cy - h), QPoint(cx - h // 2, cy + h), QPoint(cx + h, cy)]
    else:
        pts = [QPoint(cx - h, cy - h // 2), QPoint(cx + h, cy - h // 2), QPoint(cx, cy + h)]
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(200, 200, 200))
    painter.drawPolygon(QPolygon(pts))
    painter.restore()


def paint_collapse_cell(tl, painter, option, index):
    # BUILD 161: the triangle part of ClipDelegate.paint's COLLAPSE_COL
    # branch (the delegate still paints the cell with super().paint first)
    name_item = tl.table.item(index.row(), 0)
    coll = bool(name_item.data(ROLE_COLLAPSED)) if name_item is not None else False
    paint_collapse_triangle(painter, option.rect, coll)


def _setup_collapse_column(self):
    # BUILD 161: the collapse column / collapsed row lines of
    # SequenceTimeline.__init__ (called at the same spot)
    # Fixed 18 px triangle column (the header's font-based minimum
    # section size would widen it) and 16 px collapsed rows (same for
    # the hidden vertical header). Both minimums only matter below the
    # sizes the other columns / rows already use.
    self.ruler.setMinimumSectionSize(min(self.ruler.minimumSectionSize(),
                                         COLLAPSE_COL_WIDTH))
    self.table.verticalHeader().setMinimumSectionSize(
        min(self.table.verticalHeader().minimumSectionSize(), COLLAPSED_ROW_HEIGHT))
    self.ruler.setSectionResizeMode(COLLAPSE_COL, QHeaderView.Fixed)
    self.ruler.resizeSection(COLLAPSE_COL, COLLAPSE_COL_WIDTH)
    self.ruler.moveSection(self.ruler.visualIndex(COLLAPSE_COL), 0)


def _toggle_collapse_at(self, pos):
    # BUILD 126: click in the triangle cell -> toggle tracks.collapsed
    # (same path as Disable / Solo: no undo step, refresh)
    if self.table.columnAt(pos.x()) != COLLAPSE_COL:
        return False
    row = self.table.rowAt(pos.y())
    if row < 0:
        return False
    item = self.table.item(row, 0)
    tid = item.data(Qt.UserRole) if item is not None else None
    if tid is None:
        return False
    self._toggle_track_flag(tid, "collapsed")
    return True


# ---------------------------------------------------------------- row height

def _track_row_height(self, track_id):
    # Icon height for a track, equal to the delegate's option.rect.height().
    # QTableView trims the grid line (1px) off each cell's rect, so the
    # painted height is rowHeight - 1 when the grid is shown. Using plain
    # rowHeight here made the out icon cache key miss paint's key.
    # BUILD 126: a collapsed track draws no icons; icons are made for
    # its open height so they are ready when it opens
    # BUILD 128: open clips draw thumbnails in the content rect below
    # the title bar, so the icon height is the content height
    from keyframe import title_heights
    grid = 1 if self.table.showGrid() else 0
    for r in range(self.table.rowCount()):
        item = self.table.item(r, 0)
        if item is not None and item.data(Qt.UserRole) == track_id:
            h = self.table.rowHeight(r)
            if item.data(ROLE_COLLAPSED):
                h = int(item.data(ROLE_OPEN_HEIGHT) or DEFAULT_TRACK_HEIGHT)
            return title_heights(max(0, h - grid))[1]
    return 0


# ---------------------------------------------------------------- menus

# BUILD 123: the Tracks menu items, in order: (label, handler name).
# Shared by the Tracks menu bar menu and the empty-space right-click.
TRACK_ADD_ITEMS = (
    ("+ Video", "_add_video_track"),
    ("+ Audio", "_add_audio_track"),
    ("+ Image", "_add_image_track"),
    ("+ Caption", "_add_caption_track"),
    ("+ Keyframe", "_add_keyframe_track"),
)


def _track_add_items(self):
    # [(label, bound handler)] (handlers take no arguments)
    out = []
    for label, name in self.TRACK_ADD_ITEMS:
        fn = getattr(self, name)
        out.append((label, lambda checked=False, f=fn: f()))
    return out


def _add_track_actions(self, menu):
    # Add the Tracks menu items to menu; returns {action: handler}
    acts = {}
    for label, name in self.TRACK_ADD_ITEMS:
        acts[menu.addAction(label)] = getattr(self, name)
    return acts


def _add_track_menu_items(self, menu, name_item, enabled):
    # BUILD 161: the track right-click items of _track_context_menu, added
    # to menu at the same spot; returns their actions by name
    # BUILD 124: Move > Up / Down / To Top / To Bottom (Up = toward the
    # top row, smaller sort_order). Never disabled, like the old Move
    # Up / Move Down items; at an end they do nothing.
    move_menu = menu.addMenu("Move")
    up_action = move_menu.addAction("Up")
    down_action = move_menu.addAction("Down")
    top_action = move_menu.addAction("To Top")
    bottom_action = move_menu.addAction("To Bottom")
    enable_action = menu.addAction("Disable" if enabled else "Enable")
    solo_action = menu.addAction("Solo")
    solo_action.setCheckable(True)
    solo_action.setChecked(bool(name_item.data(Qt.UserRole + 2)))
    display_action = menu.addAction("Track Display Properties")
    return {"up": up_action, "down": down_action, "top": top_action,
            "bottom": bottom_action, "enable": enable_action,
            "solo": solo_action, "display": display_action}


def _run_track_menu_action(self, action, acts, track_id):
    # BUILD 161: the matching dispatch of _track_context_menu (same order);
    # True when action was one of acts
    up_action, down_action = acts["up"], acts["down"]
    top_action, bottom_action = acts["top"], acts["bottom"]
    enable_action, solo_action = acts["enable"], acts["solo"]
    display_action = acts["display"]
    if action == up_action:
        self._move_track(track_id, -1)
    elif action == down_action:
        self._move_track(track_id, 1)
    elif action == top_action:
        self._move_track_to(track_id, "top")
    elif action == bottom_action:
        self._move_track_to(track_id, "bottom")
    elif action == enable_action:
        self._toggle_track_flag(track_id, "enabled")
    elif action == solo_action:
        self._toggle_track_flag(track_id, "solo")
    elif action == display_action:
        self._track_display_properties(track_id)
    else:
        return False
    return True


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

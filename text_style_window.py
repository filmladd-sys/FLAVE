# text_style_window.py
# WYSIWYG Text style window: a floating, non-modal tool window
# of a SequenceTimeline, shown while exactly one text clip is selected
# (linked clips of other tracks may be selected with it) and hidden
# otherwise. Fields: font family (QFontComboBox), size (sequence px), color
# (button -> QColorDialog, swatch shows the color) and the plain text.
# Every change is written to text_clips at once (text_clip.write_text_field)
# as an undo step; typing in the text field, or in the size box, is one
# undo step per focus of that field. The timeline label (first line of the
# text) repaints at once. The window position is kept in the global
# settings (POS_KEY). Each change also schedules a preview
# retarget (schedule_text_preview), which marks the clip's preview frames
# stale. A 'Drop Shadow' group (checkable = shadow_on) with
# the shadow color (button -> QColorDialog with alpha, swatch; stored
# '#RRGGBBAA'), blur radius, angle (degrees clockwise from up, 135 =
# down-right; text_clip.shadow_delta) and offset (sequence px). The on
# switch and the color are one undo step each; the blur, angle and offset
# boxes are one undo step per focus like the size box. Every
# style change (font, size, color, shadow fields; not the text) also stores
# the clip's whole style as the last used text style in the global
# settings (text_clip.remember_text_style), the style of the next new text
# clip. ASCII only.

import sqlite3
from PySide6.QtCore import Qt, QEvent, QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QHBoxLayout, QFontComboBox, QSpinBox, QPushButton,
    QLabel, QPlainTextEdit, QColorDialog, QGroupBox,
)

POS_KEY = "text_style_window_pos"
SIZE_MIN = 1
SIZE_MAX = 2000
SHADOW_BLUR_MAX = 500
SHADOW_OFFSET_MAX = 2000


class TextStyleWindow(QWidget):
    def __init__(self, timeline):
        super().__init__(timeline, Qt.Tool)
        self.timeline = timeline
        self.clip_id = None
        self._undo_field = None   # field whose edits share the last undo step
        self._pos_restored = False
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Text Style")
        form = QFormLayout(self)
        self.font_box = QFontComboBox()
        form.addRow("Font:", self.font_box)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(SIZE_MIN, SIZE_MAX)
        self.size_spin.setSuffix(" px")
        form.addRow("Size:", self.size_spin)
        row = QHBoxLayout()
        self.color_btn = QPushButton("Color...")
        self.swatch = QLabel()
        self.swatch.setFixedSize(40, 20)
        row.addWidget(self.color_btn)
        row.addWidget(self.swatch)
        row.addStretch(1)
        form.addRow("Color:", row)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setTabChangesFocus(True)
        form.addRow("Text:", self.text_edit)
        # Drop shadow
        self.shadow_box = QGroupBox("Drop Shadow")
        self.shadow_box.setCheckable(True)
        self.shadow_box.setChecked(False)
        sform = QFormLayout(self.shadow_box)
        srow = QHBoxLayout()
        self.shadow_color_btn = QPushButton("Color...")
        self.shadow_swatch = QLabel()
        self.shadow_swatch.setFixedSize(40, 20)
        srow.addWidget(self.shadow_color_btn)
        srow.addWidget(self.shadow_swatch)
        srow.addStretch(1)
        sform.addRow("Color:", srow)
        self.shadow_blur_spin = QSpinBox()
        self.shadow_blur_spin.setRange(0, SHADOW_BLUR_MAX)
        self.shadow_blur_spin.setSuffix(" px")
        sform.addRow("Blur:", self.shadow_blur_spin)
        self.shadow_angle_spin = QSpinBox()
        self.shadow_angle_spin.setRange(0, 359)
        self.shadow_angle_spin.setWrapping(True)
        self.shadow_angle_spin.setSuffix(" deg")
        self.shadow_angle_spin.setToolTip(
            "Direction the shadow falls, clockwise from up (135 = down-right)")
        sform.addRow("Angle:", self.shadow_angle_spin)
        self.shadow_offset_spin = QSpinBox()
        self.shadow_offset_spin.setRange(0, SHADOW_OFFSET_MAX)
        self.shadow_offset_spin.setSuffix(" px")
        sform.addRow("Offset:", self.shadow_offset_spin)
        form.addRow(self.shadow_box)
        self._color = "#ffffff"
        self._shadow_color = "#000000B4"
        self.font_box.currentFontChanged.connect(
            lambda f: self._apply("font_family", f.family(), False))
        self.size_spin.valueChanged.connect(
            lambda v: self._apply("font_size", int(v), True))
        self.color_btn.clicked.connect(self._pick_color)
        self.text_edit.textChanged.connect(
            lambda: self._apply("text", self.text_edit.toPlainText(), True))
        self.shadow_box.toggled.connect(
            lambda on: self._apply("shadow_on", 1 if on else 0, False))
        self.shadow_color_btn.clicked.connect(self._pick_shadow_color)
        self.shadow_blur_spin.valueChanged.connect(
            lambda v: self._apply("shadow_blur", int(v), True))
        self.shadow_angle_spin.valueChanged.connect(
            lambda v: self._apply("shadow_angle", int(v), True))
        self.shadow_offset_spin.valueChanged.connect(
            lambda v: self._apply("shadow_offset", int(v), True))
        # Focus-in on a field starts a new undo step for that field
        for w in (self.size_spin, self.text_edit, self.shadow_blur_spin,
                  self.shadow_angle_spin, self.shadow_offset_spin):
            w.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self._undo_field = None
        return super().eventFilter(obj, event)

    # ---- show / load

    def set_clip(self, clip_id):
        # Show the window for clip_id (fields from the DB; a field already
        # showing the stored value is left alone)
        from text_clip import read_text_style
        style = read_text_style(self.timeline._project_db(), clip_id)
        if style is None:
            self.hide_window()
            return
        if clip_id != self.clip_id:
            self._undo_field = None
        self.clip_id = clip_id
        self._set_quiet(style)
        if not self.isVisible():
            self._restore_pos()
            self.show()

    def hide_window(self):
        self.clip_id = None
        self._undo_field = None
        if self.isVisible():
            self.hide()

    def _set_quiet(self, style):
        from text_clip import default_font_family, DEFAULT_COLOR
        from PySide6.QtGui import QFont
        family = style.get("font_family") or default_font_family()
        if self.font_box.currentFont().family() != family:
            self.font_box.blockSignals(True)
            self.font_box.setCurrentFont(QFont(family))
            self.font_box.blockSignals(False)
        size = int(style.get("font_size") or SIZE_MIN)
        if self.size_spin.value() != size:
            self.size_spin.blockSignals(True)
            self.size_spin.setValue(size)
            self.size_spin.blockSignals(False)
        self._show_color(style.get("color") or DEFAULT_COLOR)
        text = style.get("text") or ""
        if self.text_edit.toPlainText() != text:
            self.text_edit.blockSignals(True)
            self.text_edit.setPlainText(text)
            self.text_edit.blockSignals(False)
        # Shadow fields (None: the text_clip defaults)
        import text_clip as tc
        on = bool(style.get("shadow_on"))
        if self.shadow_box.isChecked() != on:
            self.shadow_box.blockSignals(True)
            self.shadow_box.setChecked(on)
            self.shadow_box.blockSignals(False)
        self._show_shadow_color(style.get("shadow_color") or tc.DEFAULT_SHADOW_COLOR)
        for spin, key, default in (
                (self.shadow_blur_spin, "shadow_blur", tc.DEFAULT_SHADOW_BLUR),
                (self.shadow_angle_spin, "shadow_angle", tc.DEFAULT_SHADOW_ANGLE),
                (self.shadow_offset_spin, "shadow_offset", tc.DEFAULT_SHADOW_OFFSET)):
            v = style.get(key)
            v = default if v is None else int(v)
            if spin.value() != v:
                spin.blockSignals(True)
                spin.setValue(v)
                spin.blockSignals(False)

    def _show_color(self, color):
        self._color = color
        self.swatch.setStyleSheet(
            "background-color: %s; border: 1px solid #808080;" % color)

    def _show_shadow_color(self, color):
        from text_clip import parse_rgba
        self._shadow_color = color
        self.shadow_swatch.setStyleSheet(
            "background-color: rgba(%d, %d, %d, %d); border: 1px solid #808080;"
            % parse_rgba(color))

    # ---- edits

    def _pick_shadow_color(self):
        # Shadow color with alpha, stored '#RRGGBBAA'
        from text_clip import parse_rgba
        c = QColorDialog.getColor(QColor(*parse_rgba(self._shadow_color)), self,
                                  "Shadow Color", QColorDialog.ShowAlphaChannel)
        if c.isValid():
            name = "#%02X%02X%02X%02X" % (c.red(), c.green(), c.blue(), c.alpha())
            self._show_shadow_color(name)
            self._apply("shadow_color", name, False)

    def _pick_color(self):
        c = QColorDialog.getColor(QColor(self._color), self, "Text Color")
        if c.isValid():
            name = c.name()    # '#rrggbb'
            self._show_color(name)
            self._apply("color", name, False)

    def _apply(self, key, value, coalesce):
        # Write one field of the clip: a new undo step unless coalesce and
        # the last step was this field (same focus); then the timeline label
        apply_text_field(self.timeline, self.clip_id, key, value,
                         push=not (coalesce and self._undo_field == key))
        self._undo_field = key if coalesce else None

    # ---- position

    def _restore_pos(self):
        if self._pos_restored:
            return
        self._pos_restored = True
        try:
            from global_settings import _get_global_setting
            x, y = (int(v) for v in (_get_global_setting(POS_KEY, "") or "").split(","))
            self.move(QPoint(x, y))
        except Exception:
            pass      # first use: the window manager places it

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.isVisible():
            try:
                from global_settings import _set_global_setting
                _set_global_setting(POS_KEY, "%d,%d" % (self.x(), self.y()))
            except Exception:
                pass


def apply_text_field(tl, clip_id, key, value, push=True):
    # One style field of one text clip into text_clips (push: one undo step
    # first), then the clip's timeline label (first line) and a repaint
    from text_clip import write_text_field, first_line
    project_db = tl._project_db()
    if clip_id is None or not project_db:
        return False
    if push:
        from undo import push_undo
        push_undo(project_db, "Text style")
    try:
        conn = sqlite3.connect(project_db)
        try:
            write_text_field(conn.cursor(), clip_id, key, value)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        tl.status_bar.showMessage(f"Could not save text clip: {e}", 3000)
        return False
    # A style change: the clip's whole style is the last used
    # text style (global settings; Add Text Clip uses it)
    from text_clip import REMEMBER_KEYS, read_text_style, remember_text_style
    if key in REMEMBER_KEYS:
        style = read_text_style(project_db, clip_id)
        if style is not None:
            remember_text_style(style)
    if key == "text":
        for clips in (getattr(tl, "clips_by_track", None) or {}).values():
            for c in clips:
                if c[0] == clip_id:
                    c[4] = first_line(value)
        tl.table.viewport().update()
    schedule_text_preview(tl)
    return True


# A style edit changes the burned-in picture: one preview
# retarget (new snapshot, mile marker diff marks the clip's frames dirty,
# the shown frame redraws) PREVIEW_DELAY_MS after the last edit, so typing
# does not snapshot the timeline per key
PREVIEW_DELAY_MS = 150


def schedule_text_preview(tl):
    from PySide6.QtCore import QTimer
    timer = getattr(tl, "_text_preview_timer", None)
    if timer is None:
        timer = QTimer(tl)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: _text_preview_now(tl))
        tl._text_preview_timer = timer
    timer.start(PREVIEW_DELAY_MS)


def _text_preview_now(tl):
    try:
        tl._retarget_preview(force=True)
    except Exception as e:
        print(f"Text preview refresh failed: {e}")


def selected_text_clip_id(tl):
    # The one selected text clip id, or None (none, or more than one)
    from text_clip import is_text_clip_id
    ids = [i for i in (getattr(tl, "_selected_clip_ids", None) or ())
           if is_text_clip_id(i)]
    return ids[0] if len(ids) == 1 else None


def sync_text_style_window(tl):
    # Selection changed: show the window for exactly one selected text
    # clip, else hide it (made on first use)
    clip_id = selected_text_clip_id(tl)
    win = getattr(tl, "_text_style_window", None)
    if clip_id is None:
        if win is not None:
            win.hide_window()
        return
    if win is None:
        win = TextStyleWindow(tl)
        tl._text_style_window = win
    win.set_clip(clip_id)

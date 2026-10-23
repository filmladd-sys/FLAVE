# text_style_window.py
# WYSIWYG Text style window (BUILD 308): a floating, non-modal tool window
# of a SequenceTimeline, shown while exactly one text clip is selected
# (linked clips of other tracks may be selected with it) and hidden
# otherwise. Fields: font family (QFontComboBox), size (sequence px), color
# (button -> QColorDialog, swatch shows the color) and the plain text.
# Every change is written to text_clips at once (text_clip.write_text_field)
# as an undo step; typing in the text field, or in the size box, is one
# undo step per focus of that field. The timeline label (first line of the
# text) repaints at once. The window position is kept in the global
# settings (POS_KEY). ASCII only.

import sqlite3
from PySide6.QtCore import Qt, QEvent, QPoint
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QFormLayout, QHBoxLayout, QFontComboBox, QSpinBox, QPushButton,
    QLabel, QPlainTextEdit, QColorDialog,
)

POS_KEY = "text_style_window_pos"
SIZE_MIN = 1
SIZE_MAX = 2000


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
        self._color = "#ffffff"
        self.font_box.currentFontChanged.connect(
            lambda f: self._apply("font_family", f.family(), False))
        self.size_spin.valueChanged.connect(
            lambda v: self._apply("font_size", int(v), True))
        self.color_btn.clicked.connect(self._pick_color)
        self.text_edit.textChanged.connect(
            lambda: self._apply("text", self.text_edit.toPlainText(), True))
        # Focus-in on a field starts a new undo step for that field
        for w in (self.size_spin, self.text_edit):
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

    def _show_color(self, color):
        self._color = color
        self.swatch.setStyleSheet(
            "background-color: %s; border: 1px solid #808080;" % color)

    # ---- edits

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
    if key == "text":
        for clips in (getattr(tl, "clips_by_track", None) or {}).values():
            for c in clips:
                if c[0] == clip_id:
                    c[4] = first_line(value)
        tl.table.viewport().update()
    return True


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

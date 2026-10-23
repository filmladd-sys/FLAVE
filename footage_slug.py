# footage_slug.py
# Slug generators for the Footage Manager's Slug menu: Color Bar and
# Color Fill. Each opens a dialog (resolution, file name; Color Fill also
# RGB), renders a PNG with QImage / QPainter, saves it in the project
# folder (the folder holding the project .db) and imports it through
# footage_importer.import_footage, the same path as Import.
# ASCII only.

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QWidget)

RESOLUTION_PRESETS = ((1920, 1080), (3840, 2160), (1280, 720))
CUSTOM_LABEL = "Custom"
MAX_SIDE = 16384

# Color bars: the classic SMPTE EG 1-1990 layout with 7.5 IRE setup, as
# drawn in the widely used Wikipedia "SMPTE Color Bars" chart (8-bit RGB).
# Top 2/3: seven 75% bars. Next 1/12: reverse castellations (each bar's
# complement chip alternating with black). Bottom 1/4: -I, 100% white, +Q
# and black, each 5/4 of a bar wide, then the PLUGE (sub-black, black,
# above-black) in the sixth bar's span and black under the seventh.
BAR_TOP = ((192, 192, 192), (192, 192, 0), (0, 192, 192), (0, 192, 0),
           (192, 0, 192), (192, 0, 0), (0, 0, 192))
BAR_BLACK = (19, 19, 19)
BAR_MIDDLE = ((0, 0, 192), BAR_BLACK, (192, 0, 192), BAR_BLACK,
              (0, 192, 192), BAR_BLACK, (192, 192, 192))
BAR_BOTTOM = ((0, 33, 76), (255, 255, 255), (50, 0, 106), BAR_BLACK)
BAR_PLUGE = ((9, 9, 9), BAR_BLACK, (29, 29, 29))


def _x(width, bars):
    # Pixel column of a position measured in bar widths (7 bars per width)
    return int(round(width * bars / 7.0))


def color_bars_image(width, height):
    # QImage (RGB888) of the color bar layout above at width x height
    img = QImage(int(width), int(height), QImage.Format_RGB888)
    img.fill(QColor(*BAR_BLACK))
    y1 = int(round(height * 2.0 / 3.0))
    y2 = int(round(height * 3.0 / 4.0))
    p = QPainter(img)
    try:
        def fill(b0, b1, ya, yb, rgb):
            x0, x1 = _x(width, b0), _x(width, b1)
            if x1 > x0 and yb > ya:
                p.fillRect(x0, ya, x1 - x0, yb - ya, QColor(*rgb))
        for i in range(7):
            fill(i, i + 1, 0, y1, BAR_TOP[i])
            fill(i, i + 1, y1, y2, BAR_MIDDLE[i])
        for i, rgb in enumerate(BAR_BOTTOM):
            fill(i * 1.25, (i + 1) * 1.25, y2, height, rgb)
        for i, rgb in enumerate(BAR_PLUGE):
            fill(5 + i / 3.0, 5 + (i + 1) / 3.0, y2, height, rgb)
        fill(6, 7, y2, height, BAR_BLACK)
    finally:
        p.end()
    return img


def color_fill_image(width, height, rgb):
    # QImage (RGB888) filled with one color
    img = QImage(int(width), int(height), QImage.Format_RGB888)
    img.fill(QColor(*rgb))
    return img


def fill_name(rgb):
    # Default Color Fill file name: fill_RRGGBB (hex)
    return "fill_%02x%02x%02x" % tuple(int(c) for c in rgb)


def png_name(name):
    # File name with a .png extension (a different extension is kept and
    # .png appended); None when empty or containing a folder separator
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    if not name.lower().endswith(".png"):
        name += ".png"
    return name


def slug_folder(project_db):
    # Generated slugs go in the project folder itself (FLAVE has no
    # generated-media subfolder; render output defaults there too)
    return os.path.dirname(os.path.abspath(project_db))


class ResolutionRow(QWidget):
    # Preset combo plus W / H spin boxes; picking a preset sets the spins,
    # editing a spin to a non-preset size shows Custom
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        for w, h in RESOLUTION_PRESETS:
            self.combo.addItem("%dx%d" % (w, h))
        self.combo.addItem(CUSTOM_LABEL)
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, MAX_SIDE)
        self.w.setValue(RESOLUTION_PRESETS[0][0])
        self.h.setValue(RESOLUTION_PRESETS[0][1])
        lay.addWidget(self.combo)
        lay.addWidget(QLabel("W"))
        lay.addWidget(self.w)
        lay.addWidget(QLabel("H"))
        lay.addWidget(self.h)
        self.combo.currentIndexChanged.connect(self._preset)
        self.w.valueChanged.connect(self._spun)
        self.h.valueChanged.connect(self._spun)

    def _preset(self, index):
        if not 0 <= index < len(RESOLUTION_PRESETS):
            return
        size = RESOLUTION_PRESETS[index]
        for s, v in ((self.w, size[0]), (self.h, size[1])):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def _spun(self, _value):
        size = (self.w.value(), self.h.value())
        i = self.combo.findText(CUSTOM_LABEL)
        if size in RESOLUTION_PRESETS:
            i = RESOLUTION_PRESETS.index(size)
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(i)
        self.combo.blockSignals(False)

    def resolution(self):
        return self.w.value(), self.h.value()


class SlugDialog(QDialog):
    # Resolution and file name; with_color adds R / G / B spins, a swatch
    # and a Pick... button (QColorDialog). The file name follows the color
    # (fill_RRGGBB) until the user edits it.
    def __init__(self, title, default_name, with_color=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
        self.rgb_spins = []
        self._auto_name = default_name if with_color else None
        if with_color:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            for label in ("R", "G", "B"):
                s = QSpinBox()
                s.setRange(0, 255)
                s.setValue(0)
                s.valueChanged.connect(self._color_changed)
                lay.addWidget(QLabel(label))
                lay.addWidget(s)
                self.rgb_spins.append(s)
            self.swatch = QLabel()
            self.swatch.setFixedSize(40, 20)
            lay.addWidget(self.swatch)
            pick = QPushButton("Pick...")
            pick.clicked.connect(self._pick)
            lay.addWidget(pick)
            form.addRow("Color", row)
        self.resolution = ResolutionRow()
        form.addRow("Resolution", self.resolution)
        self.name_edit = QLineEdit(default_name)
        form.addRow("File name", self.name_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if with_color:
            self._color_changed()

    def rgb(self):
        return tuple(s.value() for s in self.rgb_spins)

    def _color_changed(self, _value=None):
        rgb = self.rgb()
        self.swatch.setStyleSheet(
            "background-color: rgb(%d, %d, %d); border: 1px solid gray;" % rgb)
        if self.name_edit.text() == self._auto_name:
            self._auto_name = fill_name(rgb)
            self.name_edit.setText(self._auto_name)

    def _pick(self):
        c = QColorDialog.getColor(QColor(*self.rgb()), self, "Fill Color")
        if c.isValid():
            for s, v in zip(self.rgb_spins, (c.red(), c.green(), c.blue())):
                s.setValue(v)

    def accept(self):
        if png_name(self.name_edit.text()) is None:
            QMessageBox.warning(self, self.windowTitle(),
                                "Enter a file name (no folder separators).")
            return
        super().accept()


def _save_and_import(manager, title, img, name):
    # Save img as name in the project folder (asks before overwriting),
    # then import it like Import does. Returns the new footage id or None.
    from footage_importer import import_footage
    path = os.path.join(slug_folder(manager.project_db), name)
    if os.path.exists(path):
        answer = QMessageBox.question(
            manager, title, "%s already exists.\nOverwrite it?" % path,
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return None
    if not img.save(path, "PNG"):
        QMessageBox.warning(manager, title, "Could not write %s" % path)
        return None
    new_id = import_footage(manager.project_db, path)
    if not new_id:
        QMessageBox.warning(manager, title, "Could not import %s" % path)
        return None
    manager.status_bar.showMessage("Imported %s" % name)
    manager.refresh()
    return new_id


def _run(manager, title, default_name, with_color, make_image):
    if not manager.project_db:
        QMessageBox.warning(manager, title, "No project open")
        return None
    dlg = SlugDialog(title, default_name, with_color, manager)
    if dlg.exec() != QDialog.Accepted:
        return None
    w, h = dlg.resolution.resolution()
    img = make_image(w, h, dlg.rgb())
    return _save_and_import(manager, title, img, png_name(dlg.name_edit.text()))


def color_bar(manager):
    # Slug > Color Bar (manager: the FootageManager window)
    return _run(manager, "Color Bar", "color_bars", False,
                lambda w, h, _rgb: color_bars_image(w, h))


def color_fill(manager):
    # Slug > Color Fill (default black)
    return _run(manager, "Color Fill", fill_name((0, 0, 0)), True,
                color_fill_image)

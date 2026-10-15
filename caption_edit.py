# caption_edit.py
# Add / Edit Caption dialog for Ladd Video Editor
# Text, font family, font size, position X/Y and scale for one caption.
# ASCII only.

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QPlainTextEdit, QLineEdit,
    QDialogButtonBox, QMessageBox
)

DEFAULT_FONT_FAMILY = "Arial"
DEFAULT_FONT_SIZE = 48
DEFAULT_SCALE = 1.0


class CaptionEditDialog(QDialog):
    # Used for both Add (values None) and Edit (values dict from captions row)
    def __init__(self, parent=None, values=None, seq_width=1920, seq_height=1080):
        super().__init__(parent)
        self.setWindowTitle("Edit Caption" if values else "Add Caption")
        values = values or {}
        try:
            seq_width = int(seq_width) if seq_width else 1920
            seq_height = int(seq_height) if seq_height else 1080
        except (TypeError, ValueError):
            seq_width, seq_height = 1920, 1080
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.text_edit = QPlainTextEdit(values.get("text") or "")
        form.addRow("Text:", self.text_edit)
        self.font_edit = QLineEdit(values.get("font_family") or DEFAULT_FONT_FAMILY)
        form.addRow("Font family:", self.font_edit)
        self.size_edit = QLineEdit(str(self._pick(values, "font_size", DEFAULT_FONT_SIZE)))
        form.addRow("Font size:", self.size_edit)
        self.x_edit = QLineEdit(str(self._pick(values, "pos_x", seq_width // 2)))
        form.addRow("Position X:", self.x_edit)
        self.y_edit = QLineEdit(str(self._pick(values, "pos_y", seq_height // 2)))
        form.addRow("Position Y:", self.y_edit)
        self.scale_edit = QLineEdit(str(self._pick(values, "scale", DEFAULT_SCALE)))
        form.addRow("Scale:", self.scale_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(420, 320)

    @staticmethod
    def _pick(values, key, default):
        v = values.get(key)
        return default if v is None else v

    def _accept(self):
        if self.values() is None:
            QMessageBox.warning(
                self, self.windowTitle(),
                "Text is required. Font size, X and Y must be whole numbers; "
                "scale must be a number greater than 0.")
            return
        self.accept()

    def values(self):
        # dict of the captions columns, or None if any field is invalid
        text = self.text_edit.toPlainText()
        if not text.strip():
            return None
        try:
            font_size = int(self.size_edit.text().strip())
            pos_x = int(self.x_edit.text().strip())
            pos_y = int(self.y_edit.text().strip())
            scale = float(self.scale_edit.text().strip())
        except ValueError:
            return None
        if font_size <= 0 or scale <= 0:
            return None
        return {
            "text": text,
            "font_family": self.font_edit.text().strip() or DEFAULT_FONT_FAMILY,
            "font_size": font_size,
            "pos_x": pos_x,
            "pos_y": pos_y,
            "scale": scale,
        }

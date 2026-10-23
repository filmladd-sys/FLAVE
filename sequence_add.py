# sequence_add.py
# Add Sequence dialog for Ladd Video Editor
# Inserts a new row into the current project's sequences table.
# ASCII only.

import os
import sqlite3
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QComboBox,
    QPushButton, QHBoxLayout, QVBoxLayout, QMessageBox
)


# Global settings store (globalsettings.db): owned by global_settings.py
from global_settings import _get_global_setting


def next_sequence_name(project_db):
    # Sequence01, Sequence02, ...: lowest NN not used by a sequence name
    used = set()
    try:
        conn = sqlite3.connect(project_db)
        try:
            for (name,) in conn.execute("SELECT name FROM sequences").fetchall():
                name = (name or "").strip()
                if name.lower().startswith("sequence") and name[8:].isdigit():
                    used.add(int(name[8:]))
        finally:
            conn.close()
    except Exception:
        pass
    n = 1
    while n in used:
        n += 1
    return "Sequence%02d" % n


class SequenceAddDialog(QDialog):
    def __init__(self, parent=None, project_db=None, sequence_id=None):
        super().__init__(parent)
        self.sequence_id = sequence_id
        self.edit_mode = sequence_id is not None
        self.setWindowTitle("Sequence Settings" if self.edit_mode else "Add Sequence")
        self.project_db = project_db or _get_global_setting("current_project", "")
        self.new_id = None
        # BUILD 316: set by a Save that changed the size (rescaled)
        self.size_changed = False
        self.rescale_stats = None
        self._loaded_size = None
        # BUILD 320: Preview Size override loaded (None = Project Default)
        self._loaded_preview = None
        self.preview_changed = False

        self.name_edit = QLineEdit()
        if not self.edit_mode:
            # Prefill the next free SequenceNN (editable)
            self.name_edit.setText(next_sequence_name(self.project_db))

        # BUILD 316: resolution drop-down (sequence_resize.PRESETS) instead
        # of Width / Height fields; item data = (width, height). A loaded
        # size that matches no preset adds a "Custom (WxH)" item at the end.
        from sequence_resize import PRESETS, DEFAULT_SIZE, preset_index
        self.size_combo = QComboBox()
        for label, w, h in PRESETS:
            self.size_combo.addItem(label, (w, h))
        self.size_combo.setCurrentIndex(preset_index(*DEFAULT_SIZE) or 0)
        self.fps_edit = QLineEdit("29.97")

        # BUILD 320: Preview Size override (sequences.preview_size); item
        # data = percent, None = Project Default (the project's Preview Size)
        from sequence_preview import PREVIEW_SIZE_CHOICES, preview_size_percent
        self.preview_combo = QComboBox()
        try:
            project_pct = preview_size_percent(self.project_db)
        except Exception:
            project_pct = 100
        self.preview_combo.addItem("Project Default (%d%%)" % project_pct, None)
        for pct in PREVIEW_SIZE_CHOICES:
            self.preview_combo.addItem("%d%%" % pct, pct)
        self.preview_combo.setCurrentIndex(0)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("Resolution", self.size_combo)
        form.addRow("FPS", self.fps_edit)
        form.addRow("Preview Size", self.preview_combo)

        create_btn = QPushButton("Save" if self.edit_mode else "Create")
        create_btn.setDefault(True)
        create_btn.clicked.connect(self._create)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(create_btn)
        buttons.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)

    def load(self):
        # Fill fields from the existing row. Returns False if not found.
        conn = sqlite3.connect(self.project_db)
        cur = conn.cursor()
        cur.execute(
            "SELECT name, width, height, fps FROM sequences WHERE id = ?",
            (self.sequence_id,)
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            return False
        name, width, height, fps = row
        self.name_edit.setText(name or "")
        self._set_size(int(width), int(height))
        self._loaded_size = (int(width), int(height))
        self.fps_edit.setText(f"{fps:g}")
        from sequence_preview import sequence_preview_size_override
        self._loaded_preview = sequence_preview_size_override(
            self.project_db, self.sequence_id)
        i = self.preview_combo.findData(self._loaded_preview)
        self.preview_combo.setCurrentIndex(i if i >= 0 else 0)
        return True

    def _set_size(self, width, height):
        # Select the preset of (width, height), else a Custom item for it
        from sequence_resize import preset_index, custom_label
        i = preset_index(width, height)
        if i is None:
            self.size_combo.addItem(custom_label(width, height),
                                    (width, height))
            i = self.size_combo.count() - 1
        self.size_combo.setCurrentIndex(i)

    def selected_preview(self):
        # BUILD 320: the chosen Preview Size override, None = Project Default
        data = self.preview_combo.currentData()
        return int(data) if data is not None else None

    def selected_size(self):
        data = self.size_combo.currentData()
        return int(data[0]), int(data[1])

    def _create(self):
        name = self.name_edit.text().strip()
        if not self.project_db:
            QMessageBox.warning(self, "Add Sequence", "No project open")
            return
        if not name:
            QMessageBox.warning(self, "Add Sequence", "Name is required")
            return
        width, height = self.selected_size()
        try:
            fps = float(self.fps_edit.text().strip())
        except ValueError:
            QMessageBox.warning(self, "Add Sequence", "FPS must be a number")
            return
        if width <= 0 or height <= 0:
            QMessageBox.warning(self, "Add Sequence", "Width and height must be greater than 0")
            return
        if not (fps > 0):
            QMessageBox.warning(self, "Add Sequence", "FPS must be greater than 0")
            return

        # BUILD 316: an existing sequence changing size. Another aspect
        # ratio is not implemented: popup, nothing saved, dialog stays open.
        # Same aspect: one undo step, then the size and every sequence-pixel
        # value (sequence_resize.rescale_sequence) in one transaction.
        old = self._loaded_size if self.edit_mode else None
        preview = self.selected_preview()
        preview_set = preview != self._loaded_preview
        resize = old is not None and old != (width, height)
        if resize:
            from sequence_resize import same_aspect, NOT_IMPLEMENTED_TEXT
            if not same_aspect(old[0], old[1], width, height):
                QMessageBox.information(self, self.windowTitle(),
                                        NOT_IMPLEMENTED_TEXT)
                return
            from undo import push_undo
            push_undo(self.project_db, "Sequence resolution %dx%d" % (
                width, height))
        try:
            conn = sqlite3.connect(self.project_db)
            cur = conn.cursor()
            if self.edit_mode:
                cur.execute(
                    "UPDATE sequences SET name = ?, width = ?, height = ?, fps = ? WHERE id = ?",
                    (name, width, height, fps, self.sequence_id)
                )
                if resize:
                    from sequence_resize import rescale_sequence
                    self.rescale_stats = rescale_sequence(
                        cur, self.sequence_id, old[0], old[1], width, height)
                    self.size_changed = True
                self.new_id = self.sequence_id
            else:
                cur.execute(
                    "INSERT INTO sequences (name, width, height, fps) VALUES (?, ?, ?, ?)",
                    (name, width, height, fps)
                )
                self.new_id = cur.lastrowid
            # BUILD 320: Preview Size override, written only when it changed
            # (Project Default on a new sequence leaves the column NULL)
            if preview_set:
                cur.execute("UPDATE sequences SET preview_size = ? WHERE id = ?",
                            (preview, self.new_id))
            conn.commit()
            conn.close()
        except Exception as e:
            # (no commit: a failed rescale leaves the sequence as it was)
            self.size_changed = False
            QMessageBox.warning(self, self.windowTitle(), f"Could not save sequence: {e}")
            return

        if preview_set and self.edit_mode:
            # BUILD 320: a new Preview Size for this sequence: wake the
            # preview worker like a Project Settings Preview Size change (its
            # size check marks this sequence's stored frames dirty)
            self.preview_changed = True
            from project_settings import _wake_preview_worker
            _wake_preview_worker()
        self.accept()


def add_sequence(parent=None, project_db=None):
    # Returns the new sequence id, or None if cancelled.
    dlg = SequenceAddDialog(parent, project_db)
    if dlg.exec() == QDialog.Accepted:
        return dlg.new_id
    return None


def refresh_open_views(project_db, sequence_id, message):
    # BUILD 316: after a size change, the open timeline of that sequence
    # reloads and its open Sequence Player is rebuilt at the new size
    # (sequence_player.sync_player_size); stored previews are marked dirty
    # by the preview worker's size check (_sync_preview_size)
    try:
        import sequence_timeline
        from sequence_player import sync_player_size
    except Exception:
        return
    for tl in list(sequence_timeline._open_windows.values()):
        try:
            if os.path.abspath(tl._project_db()) != os.path.abspath(project_db):
                continue
            if getattr(tl, "sequence_id", None) != sequence_id:
                continue
            tl._after_clip_change(message)
            sync_player_size(tl)
        except Exception as e:
            print(f"Sequence resize refresh failed: {e}")


def edit_sequence(parent=None, project_db=None, sequence_id=None):
    # Returns the sequence id if saved, or None if cancelled or not found.
    dlg = SequenceAddDialog(parent, project_db, sequence_id)
    try:
        found = dlg.load()
    except Exception as e:
        QMessageBox.warning(parent, "Sequence Settings", f"Could not load sequence: {e}")
        return None
    if not found:
        QMessageBox.warning(parent, "Sequence Settings", "Sequence not found")
        return None
    if dlg.exec() == QDialog.Accepted:
        if dlg.size_changed:
            refresh_open_views(dlg.project_db, sequence_id,
                               "Sequence resized to %dx%d" % dlg.selected_size())
        return dlg.new_id
    return None

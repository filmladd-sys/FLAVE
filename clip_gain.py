# clip_gain.py
# Gain... for audio / video clips: one dB value stored in clips.gain_db
# (REAL DEFAULT 0, new tables only). Older clips tables without the column
# read as 0 and cannot be edited. ASCII only.

import sqlite3

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox,
    QDialogButtonBox, QMessageBox,
)

GAIN_MIN_DB = -96.0
GAIN_MAX_DB = 12.0


def _has_gain_column(cur):
    cur.execute("PRAGMA table_info(clips)")
    return "gain_db" in [r[1] for r in cur.fetchall()]


def read_clip_gain(project_db, clip_id):
    # (gain_db, editable). Missing column or row: (0.0, column present)
    conn = sqlite3.connect(project_db)
    try:
        cur = conn.cursor()
        if not _has_gain_column(cur):
            return 0.0, False
        row = cur.execute("SELECT COALESCE(gain_db, 0) FROM clips WHERE id = ?",
                          (int(clip_id),)).fetchone()
        return (float(row[0]) if row else 0.0), True
    finally:
        conn.close()


class GainDialog(QDialog):
    def __init__(self, parent, value):
        super().__init__(parent)
        self.setWindowTitle("Gain")
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Gain (dB):"))
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(1)
        self.spin.setRange(GAIN_MIN_DB, GAIN_MAX_DB)
        self.spin.setSingleStep(0.5)
        self.spin.setValue(max(GAIN_MIN_DB, min(GAIN_MAX_DB, float(value))))
        row.addWidget(self.spin)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        return round(float(self.spin.value()), 1)


def edit_clip_gain(timeline, clip):
    # Right-clicked clip only (same as Enable/Disable and Solo)
    project_db = timeline._project_db()
    if not project_db or clip is None or int(clip[0]) <= 0:
        return
    try:
        current, editable = read_clip_gain(project_db, clip[0])
    except Exception as e:
        QMessageBox.warning(timeline, "Gain", "Could not read gain: %s" % e)
        return
    if not editable:
        QMessageBox.information(
            timeline, "Gain",
            "This project's clips table has no gain column (older project).")
        return
    dlg = GainDialog(timeline, current)
    if dlg.exec() != QDialog.Accepted:
        return
    new_val = dlg.value()
    if abs(new_val - round(current, 1)) < 1e-9:
        return
    from undo import push_undo
    push_undo(project_db, "Clip gain")
    try:
        conn = sqlite3.connect(project_db)
        conn.execute("UPDATE clips SET gain_db = ? WHERE id = ?",
                     (float(new_val), int(clip[0])))
        conn.commit()
        conn.close()
    except Exception as e:
        QMessageBox.warning(timeline, "Gain", "Could not save gain: %s" % e)
        return
    # Audio only: the preview WAV .sig includes gain_db, so it rebuilds on
    # the next Play; video preview frames are unaffected
    timeline._after_clip_change("Gain %.1f dB" % new_val)

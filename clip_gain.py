# clip_gain.py
# Gain... for audio / video clips: one dB value stored in clips.gain_db
# (REAL DEFAULT 0, new_project.py). ASCII only.

import sqlite3

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox,
    QDialogButtonBox, QMessageBox,
)

GAIN_MIN_DB = -96.0
# BUILD 341: Gain maximum is 120 dB
GAIN_MAX_DB = 120.0


def read_clip_gain(project_db, clip_id):
    # gain_db of the clip; 0.0 if the row is missing
    conn = sqlite3.connect(project_db)
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT COALESCE(gain_db, 0) FROM clips WHERE id = ?",
                          (int(clip_id),)).fetchone()
        return float(row[0]) if row else 0.0
    finally:
        conn.close()


def load_clip_gains(cur, sequence_id):
    # BUILD 165: {clip_id: gain_db} for this sequence's clips with a
    # non-zero gain (title bar label, waveform scale)
    cur.execute("SELECT c.id, c.gain_db FROM clips c JOIN tracks t ON t.id = c.track_id "
                "WHERE t.sequence_id = ? AND COALESCE(c.gain_db, 0) != 0",
                (sequence_id,))
    return {r[0]: float(r[1]) for r in cur.fetchall()}


def gain_label(gain_db):
    # BUILD 165: title bar suffix, e.g. " | (Gain -5db)"
    return " | (Gain %gdb)" % round(float(gain_db), 1)


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
        current = read_clip_gain(project_db, clip[0])
    except Exception as e:
        QMessageBox.warning(timeline, "Gain", "Could not read gain: %s" % e)
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

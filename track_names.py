# track_names.py
# Default names for new tracks (Video01, Audio01, ...) and the name prompt
# that pre-fills them. ASCII only.

import sqlite3

TRACK_NAME_PREFIX = {
    "video": "Video", "audio": "Audio", "image": "Image",
    "caption": "Caption", "keyframe": "Keyframe",
}


def next_default_name(track_type, existing_names):
    # Smallest NN >= 1 (two digits, more past 99) such that Prefix+NN is not
    # already a track name (case-insensitive). Unknown type: "Track".
    prefix = TRACK_NAME_PREFIX.get((track_type or "").lower(), "Track")
    taken = {(n or "").strip().lower() for n in existing_names}
    n = 1
    while ("%s%02d" % (prefix, n)).lower() in taken:
        n += 1
    return "%s%02d" % (prefix, n)


def sequence_track_names(project_db, sequence_id):
    # Names of every track in the sequence ([] if none or no table)
    try:
        conn = sqlite3.connect(project_db)
        try:
            rows = conn.execute("SELECT name FROM tracks WHERE sequence_id = ?",
                                (int(sequence_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    return [r[0] for r in rows]


def ask_track_name(parent, title, track_type, project_db, sequence_id):
    # QInputDialog "Track name:" pre-filled with the next default, all text
    # selected so typing replaces it. Returns (name, ok) like getText.
    from PySide6.QtWidgets import QInputDialog, QLineEdit
    from PySide6.QtCore import QTimer
    default = next_default_name(
        track_type, sequence_track_names(project_db, sequence_id))
    dlg = QInputDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setLabelText("Track name:")
    dlg.setTextValue(default)

    def _select():
        edit = dlg.findChild(QLineEdit)
        if edit is not None:
            edit.selectAll()
    QTimer.singleShot(0, _select)
    ok = dlg.exec() == QInputDialog.Accepted
    return dlg.textValue(), ok

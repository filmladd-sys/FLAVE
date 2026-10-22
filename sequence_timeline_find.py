# sequence_timeline_find.py
# Edit > Find... in the Sequence Timeline: a small non-modal find window
# that searches the text of every caption in the sequence (all caption
# tracks, case-insensitive substring) and moves the playhead to the start
# of each match in turn. Matches are ordered by start frame (then track
# row, then caption id). The captions table is read again on every Find
# Next / Find Previous, so edits and new captions are always included.
# Ctrl+F opens it (or brings it back with its text selected); F3 / Enter
# = Find Next, Shift+F3 = Find Previous, in the timeline and in the find
# window. Find Next goes to the match after the current one, or, on the
# first search (new text, or the playhead moved off the current match),
# to the first match at or after the playhead; both directions wrap. The
# match is scrolled into view (_jump_to_frame) and its caption clip
# selected. ASCII only.

import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton)


def caption_matches(project_db, sequence_id, text, track_rows=None):
    # [(start_frame, caption id, track_id)] whose text contains text
    # (case-insensitive), ordered by start frame, track row, id
    needle = (text or "").strip().lower()
    if not needle or not project_db:
        return []
    try:
        conn = sqlite3.connect(project_db)
        try:
            rows = conn.execute(
                "SELECT id, track_id, start_frame, text FROM captions "
                "WHERE sequence_id = ?", (int(sequence_id),)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    track_rows = track_rows or {}
    out = [(max(0, int(start or 0)), int(cid), tid)
           for cid, tid, start, body in rows if needle in (body or "").lower()]
    out.sort(key=lambda m: (m[0], track_rows.get(m[2], 1 << 30), m[1]))
    return out


def _track_rows(tl):
    # track_id -> timeline table row
    rows = {}
    for r in range(tl.table.rowCount()):
        item = tl.table.item(r, 0)
        if item is not None:
            rows[item.data(Qt.UserRole)] = r
    return rows


class CaptionFindDialog(QDialog):
    def __init__(self, tl):
        super().__init__(tl)
        self.tl = tl
        self.setWindowTitle("Find in Captions")
        self.setModal(False)
        self._last = None          # (text, caption id, start frame)
        lay = QHBoxLayout(self)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText("Caption text")
        self.edit.setMinimumWidth(220)
        self.edit.returnPressed.connect(self.find_next)
        self.edit.textChanged.connect(self._text_changed)
        prev_btn = QPushButton("Find Previous", self)
        prev_btn.setAutoDefault(False)
        prev_btn.clicked.connect(self.find_prev)
        next_btn = QPushButton("Find Next", self)
        next_btn.setAutoDefault(False)
        next_btn.clicked.connect(self.find_next)
        self.count = QLabel("", self)
        self.count.setMinimumWidth(80)
        for w in (self.edit, prev_btn, next_btn, self.count):
            lay.addWidget(w)
        QShortcut(QKeySequence(Qt.Key_F3), self, activated=self.find_next)
        QShortcut(QKeySequence("Shift+F3"), self, activated=self.find_prev)

    def open_find(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.edit.setFocus()
        self.edit.selectAll()

    def _text_changed(self, _text):
        self._last = None
        self.count.setText("")

    def find_next(self):
        self._step(1)

    def find_prev(self):
        self._step(-1)

    def _step(self, direction):
        tl = self.tl
        text = self.edit.text()
        if not text.strip():
            self.open_find()
            return
        try:
            project_db = tl._project_db()
        except Exception:
            project_db = None
        matches = caption_matches(project_db, tl.sequence_id, text, _track_rows(tl))
        if not matches:
            self._last = None
            self.count.setText("0 of 0")
            tl.status_bar.showMessage('No captions contain "%s"' % text.strip(), 4000)
            return
        n = len(matches)
        cur = int(tl.current_frame)
        idx = None
        last = self._last
        if last is not None and last[0] == text and last[2] == cur:
            for i, m in enumerate(matches):
                if m[1] == last[1]:
                    idx = (i + direction) % n
                    break
        if idx is None:
            if direction > 0:
                idx = next((i for i, m in enumerate(matches) if m[0] >= cur), 0)
            else:
                idx = next((i for i in range(n - 1, -1, -1) if matches[i][0] < cur), n - 1)
        start, cid, tid = matches[idx]
        self.count.setText("%d of %d" % (idx + 1, n))
        tl._jump_to_frame(start)
        self._last = (text, cid, int(tl.current_frame))
        self._select(cid, tid)
        tl.status_bar.showMessage('Caption %d of %d containing "%s"'
                                  % (idx + 1, n, text.strip()), 4000)

    def _select(self, cid, tid):
        # Select the caption clip and scroll its track row into view
        tl = self.tl
        try:
            clip = tl._clip_by_id(-int(cid))
            if clip is not None:
                tl._select_clip(clip, "normal")
            row = _track_rows(tl).get(tid)
            item = tl.table.item(row, 0) if row is not None else None
            if item is not None:
                tl.table.scrollToItem(item)
        except Exception:
            pass


def _dialog(tl):
    dlg = getattr(tl, "_caption_find_dialog", None)
    if dlg is None:
        dlg = CaptionFindDialog(tl)
        tl._caption_find_dialog = dlg
    return dlg


def open_caption_find(tl):
    _dialog(tl).open_find()


def caption_find_next(tl):
    # F3 in the timeline: next match of the find text, or open Find
    dlg = getattr(tl, "_caption_find_dialog", None)
    if dlg is None or not dlg.edit.text().strip():
        open_caption_find(tl)
        return
    dlg.find_next()


def caption_find_prev(tl):
    dlg = getattr(tl, "_caption_find_dialog", None)
    if dlg is None or not dlg.edit.text().strip():
        open_caption_find(tl)
        return
    dlg.find_prev()

# refresh_preview_dialog.py
# BUILD 167: "Refreshing Preview" window for F5 / Refresh Preview on the
# Sequence Timeline. Two rows: the preview frames (percent from the preview
# worker's progress, done when its explicit refresh pass finishes) and the
# audio mix (busy bar while the mix job runs; the mix reports no percent).
# Polls on the GUI thread; closes itself when both are done. Closing it
# early only hides it: the refresh keeps running. ASCII only.

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout)


class RefreshPreviewDialog(QDialog):
    def __init__(self, timeline):
        super().__init__(timeline)
        self._timeline = timeline
        self._video_pct = 0
        self.setWindowTitle("Refreshing Preview")
        self.setModal(False)
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        self._video_label = QLabel("Preview frames")
        self._video_bar = QProgressBar()
        self._video_bar.setRange(0, 100)
        self._audio_label = QLabel("Audio")
        self._audio_bar = QProgressBar()
        hide_btn = QPushButton("Hide")
        hide_btn.clicked.connect(self.close)
        for w in (self._video_label, self._video_bar, self._audio_label,
                  self._audio_bar, hide_btn):
            lay.addWidget(w)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

    def start(self):
        # (Re)start for a new refresh request
        self._video_pct = 0
        self._poll()
        self._timer.start()
        self.show()
        self.raise_()

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)

    def _video_state(self):
        # (done, percent) for the explicit refresh pass
        try:
            import sequence_preview_worker as spw
        except Exception:
            return True, 100
        if not spw._refresh_all[0]:
            return True, 100
        w = spw._worker
        prog = getattr(w, "progress", None)
        if prog is not None and prog[1] == self._timeline.sequence_id and prog[3] > 0:
            _p, _s, clean0, total = prog
            pct = int((min(total, clean0 + w._done) * 100) // total)
            self._video_pct = max(0, min(99, pct))
        return False, self._video_pct

    def _poll(self):
        v_done, v_pct = self._video_state()
        self._video_bar.setValue(v_pct)
        self._video_label.setText("Preview frames: done" if v_done
                                  else "Preview frames: generating")
        job = getattr(self._timeline, "_audio_mix_job", None)
        a_done = job is None or job.done
        if a_done:
            self._audio_bar.setRange(0, 100)
            self._audio_bar.setValue(100)
            self._audio_label.setText("Audio: done")
        else:
            self._audio_bar.setRange(0, 0)       # busy indicator
            self._audio_label.setText("Audio: mixing")
        if v_done and a_done:
            self.close()

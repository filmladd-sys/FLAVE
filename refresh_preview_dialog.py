# refresh_preview_dialog.py
# BUILD 167: "Refreshing Preview" window for F5 / Refresh Preview on the
# Sequence Timeline. Two rows: the preview frames (percent from the preview
# worker's progress, done when its explicit refresh pass finishes) and the
# audio mix (busy bar while the mix job runs; the mix reports no percent).
# Polls on the GUI thread; closes itself when both are done.
# BUILD 239: application-modal and always on top (above every FLAVE window,
# the Sequence Player included), so nothing else takes input while it is
# open; the work runs on its own threads and the event loop keeps running
# (no exec). Cancel, Esc and the title bar X all cancel the refresh: a
# pending Play start is dropped, the explicit refresh pass stops (frames
# already built stay clean, the rest stay dirty) and a running audio mix is
# cancelled. ASCII only.

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout)


class RefreshPreviewDialog(QDialog):
    def __init__(self, timeline):
        super().__init__(timeline)
        self._timeline = timeline
        self._video_pct = 0
        self.setWindowTitle("Refreshing Preview")
        # BUILD 239: blocking and on top; flags set before the first show
        self.setWindowModality(Qt.ApplicationModal)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._finished = False      # True when it closes itself (work done)
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        self._video_label = QLabel("Preview frames")
        self._video_bar = QProgressBar()
        self._video_bar.setRange(0, 100)
        self._audio_label = QLabel("Audio")
        self._audio_bar = QProgressBar()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        for w in (self._video_label, self._video_bar, self._audio_label,
                  self._audio_bar, cancel_btn):
            lay.addWidget(w)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

    def start(self):
        # (Re)start for a new refresh request
        self._video_pct = 0
        self._finished = False
        self._poll()
        if self._finished:
            return          # nothing to wait for: closed itself already
        self._timer.start()
        self.show()
        # Above a maximized / full-screen Sequence Player too
        self.raise_()
        self.activateWindow()

    def reject(self):
        # Cancel, Esc and the title bar X (QDialog's closeEvent calls this)
        # all end here; it also hides the window when the work is done
        self._timer.stop()
        if not self._finished:
            self._cancel_refresh()
        super().reject()

    def _cancel_refresh(self):
        # User cancel: stop what this window waits for
        tl = self._timeline
        try:
            tl._stop_play()
        except Exception as e:
            print(f"Refresh cancel (play) failed: {e}")
        try:
            from sequence_preview_worker import set_refresh_all
            set_refresh_all(False)
        except Exception as e:
            print(f"Refresh cancel (frames) failed: {e}")
        try:
            from sequence_preview_audio import cancel_mix
            cancel_mix(tl._project_db(), tl.sequence_id)
        except Exception as e:
            print(f"Refresh cancel (audio) failed: {e}")
        try:
            tl.status_bar.showMessage("Refresh preview cancelled", 3000)
        except Exception:
            pass

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
        # BUILD 172: GUI heartbeat for the refresh pass (it naps briefly
        # when this poll has not run for a while, so the window updates)
        try:
            import time
            import sequence_preview_worker as spw
            spw._gui_beat[0] = time.perf_counter()
        except Exception:
            pass
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
            self._finished = True
            self.close()

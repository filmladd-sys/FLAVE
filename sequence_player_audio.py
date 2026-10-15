# sequence_player_audio.py
# Plays {sequence_id}_previewaudio.wav with QMediaPlayer, locked to the
# timeline play clock (frame / fps). ASCII only.

import os

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer


class PreviewAudioPlayer(object):
    # Small helper owned by SequencePlayer. Seek by frame; resync on drift.

    def __init__(self):
        self.player = QMediaPlayer()
        self.output = QAudioOutput()
        self.player.setAudioOutput(self.output)
        self.wav_path = None
        self.fps = 29.97

    def prepare(self, wav_path, fps):
        self.fps = float(fps) if fps and float(fps) > 0 else 29.97
        path = os.path.abspath(wav_path) if wav_path else None
        if not path or not os.path.isfile(path):
            self.wav_path = None
            return False
        if path != self.wav_path:
            self.player.setSource(QUrl.fromLocalFile(path))
            self.wav_path = path
        return True

    def _frame_ms(self, frame):
        return int(round(float(frame) * 1000.0 / self.fps))

    def play_at_frame(self, frame):
        if not self.wav_path:
            return
        self.player.setPosition(self._frame_ms(frame))
        self.player.play()

    def seek_frame(self, frame):
        if not self.wav_path:
            return
        self.player.setPosition(self._frame_ms(frame))

    def pause(self):
        try:
            self.player.pause()
        except Exception:
            pass

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass

    def resync_if_drift(self, frame, threshold_ms=80):
        # If audio position is far from the video clock, snap it back.
        if not self.wav_path:
            return
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        want = self._frame_ms(frame)
        got = int(self.player.position())
        if abs(want - got) > int(threshold_ms):
            self.player.setPosition(want)

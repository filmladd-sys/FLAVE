# sequence_timeline_keyframe.py
# Keyframe track helpers for sequence_timeline.py (no window class here).
# Window methods take the SequenceTimeline instance as self. Empty track
# only: no keyframe clips yet. ASCII only.

KEYFRAME_BG_COLOR = "#403050"


def _add_keyframe_track(self):
    # Same name dialog / insert path as video/audio/image via _add_track.
    self._add_track(
        "keyframe", "+ Keyframe", bg_color=KEYFRAME_BG_COLOR)

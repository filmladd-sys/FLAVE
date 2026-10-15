# sequence_timeline_keyframe.py
# Keyframe track helpers for sequence_timeline.py (no window class here).
# Window methods take the SequenceTimeline instance as self. Empty track
# only: no keyframe clips yet. Schema helpers live in keyframe.py.
# ASCII only.


def _add_keyframe_track(self):
    # Same name dialog / insert path as video/audio/image via _add_track.
    self._add_track("keyframe", "+ Keyframe")

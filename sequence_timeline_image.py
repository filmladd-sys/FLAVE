# Image-only helpers for sequence_timeline.py (no window class here).
# Window methods take the SequenceTimeline instance as self.

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def _add_image_track(self):
    self._add_track("image", "+ Image")

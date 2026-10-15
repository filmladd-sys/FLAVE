# sequence_preview_worker.py
# One background thread that fills {sequence_id}_preview.db for the active
# timeline. Works outward from the playhead (next, previous, ...), skips clean
# frames, yields to the GUI through the Sequence Player generate lock, and
# sleeps on a condition when there is nothing to do. ASCII only.

import threading
import time

from sequence_preview import PreviewBatch, apply_dirty_queue, clean_set


def _forward(center, total):
    # Forward in order from the playhead to the end, then wrap to frame 0 and
    # fill forward up to the playhead. The decoder continues frame to frame;
    # the only seeks are at the start and at the wrap. Clipped to 0..total-1.
    if total <= 0:
        return
    center = max(0, min(total - 1, int(center)))
    for f in range(center, total):
        yield f
    for f in range(0, center):
        yield f


class PreviewWorker(object):

    def __init__(self):
        self._cond = threading.Condition()
        self._target = None      # (project, sequence_id, center, total, snapshot)
        self._gen = 0            # bumped on every retarget or wake
        self._done_gen = -1      # gen whose pass finished (sleep until bumped)
        self.saved = 0           # rows saved, read by the timeline repaint
        self.sequence_id = None
        self._thread = threading.Thread(target=self._run, name="PreviewWorker",
                                        daemon=True)
        self._thread.start()

    # ---- GUI thread

    def retarget(self, project, sequence_id, center, total, snapshot, fps=None):
        # Playhead move, edit, activation or miss: restart outward from center.
        # Order is forward only (see _forward); run = frames per save batch
        run = int(round(fps)) if fps else 30
        with self._cond:
            self._target = (project, sequence_id, int(center), int(total),
                            snapshot, max(1, run))
            self.sequence_id = sequence_id
            self._gen += 1
            self._cond.notify()

    def clear(self, sequence_id=None):
        # Timeline closed: stop working on it
        with self._cond:
            if sequence_id is None or self.sequence_id == sequence_id:
                self._target = None
                self.sequence_id = None
                self._gen += 1
                self._cond.notify()

    # ---- worker thread

    def _run(self):
        while True:
            with self._cond:
                while self._target is None or self._done_gen == self._gen:
                    self._cond.wait()
                target = self._target
                gen = self._gen
            try:
                self._pass(target, gen)
            except Exception as e:
                print(f"Preview worker failed: {e}")
            with self._cond:
                if gen == self._gen:
                    # Done, or cannot generate (no player): wait for a wake
                    self._done_gen = gen

    def _pass(self, target, gen):
        from sequence_player import FrameGenerate
        import sequence_player
        project, sequence_id, center, total, snapshot, run = target
        # Queued dirty marks from edits, written on this thread
        apply_dirty_queue()
        if total <= 0:
            return
        # One transaction per second of frames (fps, or 30 if missing)
        batch = PreviewBatch(project, sequence_id)
        try:
            self._pass_batched(FrameGenerate, sequence_player, batch, project,
                               sequence_id, center, total, snapshot, run, gen)
        finally:
            # Jump, close, end of pass or error: commit what is pending
            n = len(batch.pending)
            batch.close()
            self.saved += n

    def _gui_busy(self, sp):
        # The GUI wants the lock or was active within the idle time.
        # A player waiting on a missing preview frame is not "busy": fill it.
        # Play filling the remainder is not busy either.
        if getattr(sp, "_preview_wait", [0])[0] > 0 or _play_priority[0]:
            return False
        return (sp._gui_wants[0] > 0
                or time.perf_counter() - sp._gui_last[0] < sp._GUI_QUIET[0])

    def _pass_batched(self, FrameGenerate, sp, batch, project, sequence_id,
                      center, total, snapshot, run, gen):
        # run = frames per second (sequence fps, or 30 if missing)
        clean = clean_set(project, sequence_id)
        burst_left = 0
        batch_frames = run
        # Frames per card pass, read at the start of a pass
        gpu_n = _setting_int("previewGpuBatchFrames", 8, 1, 30)
        from sequence_player import FrameGenerateBatch
        order = list(_forward(center, total))
        i = 0
        try:
            while i < len(order):
                frame = order[i]
                if gen != self._gen:
                    return
                if frame in clean:
                    i += 1
                    continue
                if burst_left <= 0:
                    # Burst boundary: read settings, yield if the user is active
                    sp._worker_burst[0] = False
                    burst_left = _setting_int("previewBurstFrames", 15, 1, 300)
                    sp._GUI_QUIET[0] = _setting_int("previewIdleMs", 250, 0, 5000) / 1000.0
                    if self._gui_busy(sp):
                        # Yielding: commit first so no write is left open
                        if batch.pending:
                            n = len(batch.pending)
                            batch.commit()
                            self.saved += n
                        # Wait for previewIdleMs of no user activity
                        while self._gui_busy(sp):
                            if gen != self._gen:
                                return
                            time.sleep(0.01)
                    sp._worker_burst[0] = True
                if not batch.pending:
                    # Batch start: previewBatchSeconds worth of frames
                    batch_frames = run * _setting_int("previewBatchSeconds", 1, 1, 10)
                # Card pass: up to gpu_n sequential, not-clean frames, never
                # more than the burst has left (a burst still yields on time)
                chunk = [frame]
                j = i + 1
                limit = max(1, min(gpu_n, burst_left))
                while (len(chunk) < limit and j < len(order)
                       and order[j] == chunk[-1] + 1 and order[j] not in clean):
                    chunk.append(order[j])
                    j += 1
                # Let the GUI thread run between card passes (GIL and the lock)
                time.sleep(0.005)
                if len(chunk) == 1:
                    jpegs = [FrameGenerate(project, sequence_id, frame,
                                           snapshot=snapshot, worker=True)]
                else:
                    jpegs = FrameGenerateBatch(project, sequence_id, chunk, snapshot)
                if not jpegs:
                    return
                for f, jpeg in zip(chunk, jpegs):
                    if jpeg is None:
                        return
                    batch.add(f, jpeg)
                    burst_left -= 1
                    if len(batch.pending) >= batch_frames:
                        n = len(batch.pending)
                        batch.commit()
                        self.saved += n
                # The playhead frame must be readable before the rest of the
                # batch, so the player can show it without waiting
                if center in chunk and batch.pending:
                    n = len(batch.pending)
                    batch.commit()
                    self.saved += n
                i = j
        finally:
            sp._worker_burst[0] = False


def _setting_int(key, default, lo, hi):
    # Global setting as an int clamped to lo..hi; default if missing or bad
    try:
        from sequence_timeline_sql import _get_global_setting
        v = int(str(_get_global_setting(key, str(default))).strip())
    except Exception:
        return default
    return max(lo, min(hi, v))


_worker = None
# Play is waiting for the remainder to be green. Skip the idle yield.
_play_priority = [0]


def set_play_priority(on):
    # Timeline sets this while Play is filling from the tick to the end
    _play_priority[0] = 1 if on else 0


def get_worker():
    # The single preview worker, started on first use
    global _worker
    if _worker is None:
        _worker = PreviewWorker()
    return _worker

# sequence_preview_worker.py
# One background thread that fills {sequence_id}_preview.db for the active
# timeline. Works outward from the playhead (next, previous, ...), skips clean
# frames, yields to the GUI through the Sequence Player generate lock, and
# sleeps on a condition when there is nothing to do. ASCII only.
# BUILD 141: no Sequence Player needed: with none open for the sequence the
# frames are composited headless on the GPU (sequence_preview_gpu).
# BUILD 145: always on that headless composer (the one frame engine), with
# or without a player open; the player only presents.
# BUILD 166: explicit refresh model. During normal editing a pass builds
# only the playhead frame (scrub / edit stays realtime). Every not-clean
# frame (dirty or missing) is built only after an explicit refresh (F5 /
# Refresh Preview, set_refresh_all) or while Play fills (set_play_priority).

import threading
import time

from sequence_preview import (PreviewBatch, apply_dirty_queue, clean_set,
                              dirty_seq, dirty_queue_pending, _CLEAN_LOCK)


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
        # BUILD 129: (project, sequence_id, clean at pass start, total) while
        # a pass has frames to generate, else None; _done = rows committed
        # this pass. Plain attributes, read by the GUI status poll.
        self.progress = None
        self._done = 0
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

    def wake(self, sequence_id=None):
        # BUILD 141: dirty marks landed after the retarget (the snapshot diff
        # runs after it): start the pass again so a pass that already found
        # everything clean does not leave the new dirty frames waiting for
        # the next scrub (no player open = nobody else wakes the worker)
        with self._cond:
            if self._target is not None and (sequence_id is None
                                             or self.sequence_id == sequence_id):
                self._gen += 1
                self._cond.notify()

    def clear(self, sequence_id=None):
        # Timeline closed: stop working on it
        with self._cond:
            if sequence_id is None or self.sequence_id == sequence_id:
                self._target = None
                self.sequence_id = None
                self.progress = None
                self._gen += 1
                self._cond.notify()

    # ---- worker thread

    def _run(self):
        # Attach the process-wide CUDA context on this worker thread
        try:
            from cuda_context import ensure_current
            ensure_current()
        except Exception as e:
            # print("Preview worker CUDA attach failed: %s" % e)
            pass
        while True:
            idle_release = False
            with self._cond:
                while self._target is None or self._done_gen == self._gen:
                    if self._target is None and _headless_active():
                        # BUILD 141: timeline closed: free the headless GPU
                        # composite (outside the condition: it takes the
                        # generate lock)
                        idle_release = True
                        break
                    self._cond.wait()
                target = self._target
                gen = self._gen
            if idle_release:
                _release_headless()
                continue
            try:
                self._pass(target, gen)
            except Exception as e:
                print(f"Preview worker failed: {e}")
                self.progress = None
            with self._cond:
                if gen == self._gen:
                    # Done, or cannot generate (no player): wait for a wake
                    self._done_gen = gen
                    # BUILD 166: the explicit refresh pass is finished
                    _refresh_all[0] = 0
                    # Idle: the timeline status clears (BUILD 129)
                    self.progress = None

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
            self._done += n

    def _commit(self, batch):
        # Commit the open transaction; counts rows for the ruler repaint and
        # the status percent (BUILD 129)
        if batch.pending:
            n = len(batch.pending)
            batch.commit()
            self.saved += n
            self._done += n

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
        burst_t0 = 0.0
        batch_frames = run
        # Frames per card pass, read at the start of a pass
        gpu_n = _setting_int("previewGpuBatchFrames", 8, 1, 30)
        from sequence_player import FrameGenerateBatch
        # BUILD 166: whole range only on an explicit refresh or Play fill;
        # otherwise just the playhead frame
        full = bool(_refresh_all[0] or _play_priority[0])
        order = (list(_forward(center, total)) if full
                 else [max(0, min(total - 1, int(center)))])
        # BUILD 169: explicit refresh pass = fast mode: no bursts, yields,
        # GUI-quiet waits or sleeps (only a GUI waiting on the lock holds the
        # worker back), bigger card passes and fewer commits
        fast = bool(_refresh_all[0])
        if fast:
            # BUILD 170: card pass size from free VRAM, re-read per chunk
            from sequence_player_preview import refresh_batch_frames
            import sequence_player_gpu as spg
            gpu_low = gpu_n
            spg._worker_fast[0] = True
            sp._worker_burst[0] = True
            burst_left = 1 << 30
            bursts = 1
            burst_t0 = time.perf_counter()
        # BUILD 129: status percent = clean frames / total frames, from the
        # clean count in 0..total-1 at pass start plus rows committed since
        # (no db COUNT). Shown only when something needs generating.
        with _CLEAN_LOCK:
            clean0 = sum(1 for f in clean if 0 <= f < total)
        self._done = 0
        self.progress = ((project, sequence_id, clean0, total)
                         if full and clean0 < total else None)
        sec_per_frame = None     # measured card pass time per frame (EWMA)
        t_pass0 = time.perf_counter()
        made = 0
        if not fast:
            bursts = 0
        hold_max = 0.0
        retried = False
        i = 0
        try:
            while i < len(order):
                frame = order[i]
                if gen != self._gen:
                    return
                if frame in clean:
                    i += 1
                    continue
                if (not fast and burst_left > 0 and (sp._gui_wants[0] > 0
                        or time.perf_counter() - burst_t0 >= WORKER_BURST_SEC)):
                    # BUILD 129: time-based burst end (~1 s), or at once when
                    # the GUI waits on _GEN_LOCK ('scrub waiting')
                    burst_left = 0
                if burst_left <= 0:
                    # Burst boundary: commit (no write left open), let the
                    # GUI thread take the GIL and the lock, then read settings
                    # and wait while the user is active
                    sp._worker_burst[0] = False
                    if bursts:
                        self._commit(batch)
                        time.sleep(WORKER_YIELD_SEC)
                        if gen != self._gen:
                            return
                    bursts += 1
                    burst_left = _setting_int("previewBurstFrames", 15, 1, 300)
                    sp._GUI_QUIET[0] = _setting_int("previewIdleMs", 250, 0, 5000) / 1000.0
                    if self._gui_busy(sp):
                        # Yielding: commit first so no write is left open
                        self._commit(batch)
                        # Wait for previewIdleMs of no user activity
                        while self._gui_busy(sp):
                            if gen != self._gen:
                                return
                            time.sleep(0.01)
                    sp._worker_burst[0] = True
                    burst_t0 = time.perf_counter()
                if not batch.pending:
                    # Batch start: previewBatchSeconds worth of frames
                    batch_frames = run * _setting_int("previewBatchSeconds", 1, 1, 10)
                    if fast:
                        batch_frames = max(batch_frames, run * REFRESH_BATCH_SECONDS)
                # Card pass: up to gpu_n sequential, not-clean frames, never
                # more than the burst has left (a burst still yields on time)
                # and (BUILD 129) no more than WORKER_PASS_SEC of measured
                # work, so one _GEN_LOCK hold stays short
                chunk = [frame]
                j = i + 1
                left_s = WORKER_BURST_SEC - (time.perf_counter() - burst_t0)
                budget = REFRESH_PASS_SEC if fast else min(WORKER_PASS_SEC, left_s)
                if fast:
                    gpu_n = refresh_batch_frames(project, sequence_id, snapshot,
                                                 gpu_low, REFRESH_GPU_MAX)
                    time_n = pass_frame_limit(sec_per_frame, budget)
                limit = max(1, min(gpu_n, burst_left,
                                   pass_frame_limit(sec_per_frame, budget)))
                while (len(chunk) < limit and j < len(order)
                       and order[j] == chunk[-1] + 1 and order[j] not in clean):
                    chunk.append(order[j])
                    j += 1
                # Let the GUI thread run between card passes (GIL and the lock)
                if not fast:
                    time.sleep(0.005)
                elif sp._gui_wants[0] > 0:
                    # BUILD 170: no yield for activity, but a GUI thread
                    # already blocked on the lock gets it for its one frame
                    # (else the window, Refreshing Preview too, freezes)
                    time.sleep(0.001)
                # Dirty mark number before the build: an edit made while
                # this chunk renders (old snapshot) keeps its frames dirty
                seq0 = dirty_seq()
                if dirty_queue_pending():
                    # Marks or a row shift queued since the pass began: write
                    # them first (commit runs them; no open write left), and
                    # stop if an edit retargeted the worker meanwhile
                    if batch.pending:
                        self._commit(batch)
                    else:
                        apply_dirty_queue()
                    if gen != self._gen:
                        return
                t_c = time.perf_counter()
                if len(chunk) == 1:
                    jpegs = [FrameGenerate(project, sequence_id, frame,
                                           snapshot=snapshot, worker=True)]
                else:
                    jpegs = FrameGenerateBatch(project, sequence_id, chunk, snapshot)
                dt = time.perf_counter() - t_c
                hold_max = max(hold_max, dt)
                if fast and len(chunk) > 1:
                    _log_refresh_batch(chunk, gpu_n, time_n, dt)
                per = dt / len(chunk)
                sec_per_frame = per if sec_per_frame is None else (
                    0.5 * sec_per_frame + 0.5 * per)
                if ((not jpegs or any(j is None for j in jpegs))
                        and not retried and gen == self._gen):
                    # BUILD 141: once per pass, try the chunk again: a player
                    # closed mid-chunk hands over to the headless composite
                    retried = True
                    continue
                if not jpegs:
                    return
                for f, jpeg in zip(chunk, jpegs):
                    if jpeg is None:
                        return
                    batch.add(f, jpeg, seq0)
                    made += 1
                    burst_left -= 1
                    if len(batch.pending) >= batch_frames:
                        self._commit(batch)
                # The playhead frame must be readable before the rest of the
                # batch, so the player can show it without waiting
                if center in chunk and batch.pending:
                    self._commit(batch)
                i = j
        finally:
            sp._worker_burst[0] = False
            if fast:
                spg._worker_fast[0] = False
            if made:
                el = time.perf_counter() - t_pass0
                print("WORKER preview: %d frames %.1fs (%.1f fps) bursts %d "
                      "longest card pass %.0f ms" % (
                          made, el, made / el if el > 0 else 0.0, bursts,
                          hold_max * 1000.0))


# BUILD 129: the worker yields about once a second. A burst ends after
# WORKER_BURST_SEC of work (or previewBurstFrames frames, or at once when the
# GUI waits on _GEN_LOCK); then it commits and sleeps WORKER_YIELD_SEC with
# the GIL and the lock free. One card pass (one _GEN_LOCK hold) is sized to
# about WORKER_PASS_SEC from the measured time per frame.
WORKER_BURST_SEC = 1.0
WORKER_YIELD_SEC = 0.03
WORKER_PASS_SEC = 0.25
# BUILD 169: explicit refresh (F5 / Play on dirty) fast mode: card pass up
# to REFRESH_PASS_SEC of work, one commit per REFRESH_BATCH_SECONDS of
# frames. BUILD 170: frames per card pass from free VRAM
# (refresh_batch_frames), previewGpuBatchFrames .. REFRESH_GPU_MAX
REFRESH_GPU_MAX = 120
REFRESH_PASS_SEC = 5.0
REFRESH_BATCH_SECONDS = 10


def _mb(b):
    return "%.0f MB" % (float(b) / (1 << 20))


def _log_refresh_batch(chunk, gpu_n, time_n, dt):
    # BUILD 171: one console line per large refresh card pass: the chosen
    # N and why (VRAM numbers from refresh_batch_frames), the frames this
    # pass built (also limited by the time budget and the dirty run), and
    # free VRAM after the pass
    try:
        from sequence_player_preview import _last_batch_info as info, free_vram
        after = free_vram()
        if "n" in info:
            vram = ("free %s of %s, margin %s (25%%), held %s, usable %s, "
                    "per frame %.1f MB (buffer %.1f MB + %d video layers), "
                    "vram fit %d -> N %d (%s, clamp %d..%d)" % (
                        _mb(info["free"]), _mb(info["total"]), _mb(info["margin"]),
                        _mb(info["held"]), _mb(info["usable"]),
                        info["per"] / float(1 << 20), info["buf"] / float(1 << 20),
                        info["layers"], info["raw"], info["n"], info["why"],
                        info["low"], info["high"]))
        else:
            vram = "N %d (%s)" % (gpu_n, info.get("why", "?"))
        print("WORKER refresh batch: %d frames %d..%d in %.0f ms (%.1f fps) | %s "
              "| time fit %d (%.1f s budget) | free after %s" % (
                  len(chunk), chunk[0], chunk[-1], dt * 1000.0,
                  len(chunk) / dt if dt > 0 else 0.0, vram, time_n,
                  REFRESH_PASS_SEC, _mb(after[0]) if after else "?"))
    except Exception as e:
        print("WORKER refresh batch log failed: %s" % e)


def pass_frame_limit(sec_per_frame, budget=None):
    # Frames per card pass that fit the pass budget (at least 1). Before the
    # first measurement of a pass: 1 (the playhead frame, alone and first)
    budget = WORKER_PASS_SEC if budget is None else budget
    if not sec_per_frame or sec_per_frame <= 0:
        return 1
    return max(1, int(budget / sec_per_frame))


def status_text(worker, project, sequence_id):
    # 'Generating preview NN%' while the worker generates this sequence,
    # else '' (idle, other sequence, or nothing to generate). GUI thread.
    if worker is None:
        return ""
    prog = worker.progress
    if prog is None:
        return ""
    p_project, p_seq, clean0, total = prog
    if p_seq != sequence_id or total <= 0:
        return ""
    try:
        import os
        if os.path.abspath(p_project) != os.path.abspath(project):
            return ""
    except Exception:
        return ""
    pct = int((min(total, clean0 + worker._done) * 100) // total)
    return "Generating preview %d%%" % min(99, pct)


def _setting_int(key, default, lo, hi):
    # Global setting as an int clamped to lo..hi; default if missing or bad
    try:
        from sequence_timeline_sql import _get_global_setting
        v = int(str(_get_global_setting(key, str(default))).strip())
    except Exception:
        return default
    return max(lo, min(hi, v))


def _headless_active():
    try:
        from sequence_preview_gpu import headless_active
        return headless_active()
    except Exception:
        return False


def _release_headless():
    try:
        from sequence_preview_gpu import release_headless
        release_headless()
    except Exception as e:
        print(f"Preview headless GPU release failed: {e}")


_worker = None
# Play is waiting for the remainder to be green. Skip the idle yield.
_play_priority = [0]


# BUILD 166: an explicit refresh (F5) asked for every not-clean frame;
# cleared when that pass finishes
_refresh_all = [0]


def set_refresh_all(on):
    # Set under the worker lock with a gen bump, so a pass that ends now
    # (old gen) cannot clear the new request
    w = _worker
    if w is None:
        _refresh_all[0] = 1 if on else 0
        return
    with w._cond:
        _refresh_all[0] = 1 if on else 0
        w._gen += 1
        w._cond.notify()


def set_play_priority(on):
    # Timeline sets this while Play is filling from the tick to the end
    _play_priority[0] = 1 if on else 0


def get_worker():
    # The single preview worker, started on first use
    global _worker
    if _worker is None:
        _worker = PreviewWorker()
    return _worker

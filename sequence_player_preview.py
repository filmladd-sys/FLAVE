# sequence_player_preview.py
# JPEG preview path for the Sequence Player: one frame and a card batch.
# Methods take the SequenceTimeline player as self and are attached by
# sequence_player.py. ASCII only.
# BUILD 145: one frame engine. The preview worker composites every frame on
# the headless composer (sequence_preview_gpu), whether or not a Sequence
# Player is open; the player only presents (preview.db JPEGs, play ring)
# and keeps no decoders. The open player composites only as a fallback when
# the engine's GPU setup failed. The render runs the same composite_batch
# on a private composer of the same class (own decoders, originals).

import ctypes
import os
import sys
import time

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from sequence_timeline_sql import _get_global_setting
from sequence_player_gpu import (
    _GEN_LOCK, _gui_wants, _worker_acquire,
    _libs, _check, _nvjpeg_lib, _NvJpegEncoder, _new_tm, _dark_mode,
)

def frame_jpeg(self, frame, quality=90, snapshot=None, worker=False):
    # JPEG of one sequence frame, same composite as the live path. The
    # shown texture is untouched. GUI: builds into self.dst (reused if it
    # already holds the frame). Worker: gen_dst, snapshot, no GL calls.
    # nvJPEG encodes the RGB buffer on the GPU (only JPEG bytes come
    # back); without nvjpeg64_*.dll the RGB buffer is copied and Qt encodes.
    if worker:
        _worker_acquire()
    else:
        _gui_wants[0] += 1
        _GEN_LOCK.acquire()
    try:
        kind, data = self._frame_locked(frame, snapshot, worker, quality)
    finally:
        _GEN_LOCK.release()
        if not worker:
            _gui_wants[0] -= 1
    if kind == "jpeg" or data is None:
        return data
    # Qt fallback: encode the host RGB copy outside the lock
    try:
        img = QImage(data, self.seq_w, self.seq_h, self.row, QImage.Format_RGB888)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not img.save(buf, "JPG", quality):
            return None
        return bytes(buf.data())
    except Exception as e:
        print(f"Sequence Player frame_jpeg failed: {e}")
        return None

def frames_jpeg_batch(self, frames, quality, snapshot):
    # Worker: build consecutive frames in one card pass. Each video
    # source that runs in order across the batch is decoded with one
    # get_batch_frames(N) (seek only before the first frame, and only
    # if the decoder is not already there); each frame is composited
    # into gen_dst and encoded with nvJPEG (Qt fallback after the lock).
    _worker_acquire()
    try:
        items = self._frames_locked(frames, snapshot, quality)
    finally:
        _GEN_LOCK.release()
    if items is None:
        return None
    out = []
    for kind, data in items:
        if kind == "jpeg" or data is None:
            out.append(data)
            continue
        try:
            img = QImage(data, self.seq_w, self.seq_h, self.row, QImage.Format_RGB888)
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            out.append(bytes(buf.data()) if img.save(buf, "JPG", quality) else None)
        except Exception as e:
            print(f"Sequence Player frame_jpeg failed: {e}")
            out.append(None)
    return out

def _prefetch(self, frames, snapshot, clips=None):
    # {((clip_id, path), src_frame): decoded frame} per clip whose source
    # frames run consecutively across the batch (one decoder per clip).
    # BUILD 176: clips (set of clip ids): only those clips' layers
    reqs = {}
    last = {}
    if frames:
        self._prune_decoders(frames[0], snapshot)
    for fr in frames:
        for item in self._layer_items(fr, snapshot):
            kind, path, src = item[0], item[1], item[2]
            if clips is not None and item[6][0] not in clips:
                continue
            if kind == "video" and os.path.isfile(path):
                key = item[6] if len(item) > 6 else (None, path)
                reqs.setdefault(key, []).append(src)
                last[key] = fr
    pre = {}
    for key, srcs in reqs.items():
        if len(srcs) < 2 or srcs != list(range(srcs[0], srcs[0] + len(srcs))):
            continue
        dec = self._decoder(key, last.get(key))
        if self.dec_next.get(key) != srcs[0]:
            dec.seek_to_index(srcs[0])
        try:
            got = dec.get_batch_frames(len(srcs))
        except Exception:
            self.dec_next.pop(key, None)
            raise
        got = list(got or [])
        for k, f in enumerate(got):
            pre[(key, srcs[0] + k)] = f
        if got:
            self.dec_next[key] = srcs[0] + len(got)
        else:
            self.dec_next.pop(key, None)
    return pre

# BUILD 170: buffers / encoder states kept after a pass; a normal pass
# (n <= GEN_KEEP) frees what a bigger refresh pass grew beyond that
# BUILD 172: also freed at the end of every pass larger than GEN_KEEP, so
# the next free-VRAM query sees the real free memory
GEN_KEEP = 30


def _trim_gen(self):
    # Free composite buffers / nvJPEG states beyond GEN_KEEP (lock held).
    # BUILD 173: never while a refresh pool is reserved
    if getattr(self, "_refresh_hold", 0):
        return
    if len(self.gen_bufs) > GEN_KEEP:
        cudart = _libs()[0]
        for b in self.gen_bufs[GEN_KEEP:]:
            if b.value:
                cudart.cudaFree(b)
        del self.gen_bufs[GEN_KEEP:]
    if self.nvj is not None:
        self.nvj.trim(GEN_KEEP)


def _gen_buffers(self, n):
    # n sequence-size RGB device buffers for one GPU pass (grown, kept)
    cudart = _libs()[0]
    if n <= GEN_KEEP:
        _trim_gen(self)
    while len(self.gen_bufs) < n:
        ptr = ctypes.c_void_p()
        _check(cudart.cudaMalloc(ctypes.byref(ptr), self.row * self.seq_h),
               "gen buffer malloc")
        self.gen_bufs.append(ptr)
    return self.gen_bufs[:n]

def _frames_locked(self, frames, snapshot, quality):
    # One GPU pass: decode all N (one get_batch_frames per in-order
    # source), composite all N into N device buffers, nvJPEG-encode all
    # N from those buffers; only the JPEG bytes come back to the host.
    # BUILD 142: the pass itself is composite_batch (shared with the
    # render); this is its preview JPEG sink.
    if self.closed or not self.ready:
        return None
    n = len(frames)
    try:
        items = composite_batch(self, frames, snapshot, _dark_mode(),
                                self._gen_buffers(n), _jpeg_sink(quality))
        self._target = None
        if n > GEN_KEEP:
            # BUILD 172: a large refresh pass gives its extra memory back
            _trim_gen(self)
    except Exception as e:
        self._target = None
        print(f"Sequence Player frame_jpeg failed: {e}")
        items = [("jpeg", None)] * n
    return items


def composite_batch(self, frames, snapshot, dark, bufs, sink,
                    sync_name="composite sync"):
    # BUILD 142: the one frame-batch loop of the preview fill and the
    # render. Caller holds _GEN_LOCK. Decode (one get_batch_frames per
    # in-order source), composite every frame into its device buffer
    # (stream ordered, no host sync between frames), one stream sync, then
    # sink(self, bufs) turns the N device buffers into the result list.
    # self: a SequencePlayer or a HeadlessComposer. Raises on CUDA errors.
    cudart = _libs()[0]
    nvcuda = _libs()[1]
    self._tm = _new_tm()
    _check(nvcuda.cuCtxSetCurrent(self.cuda_ctx), "ctx")
    if getattr(self, "_refresh_hold", 0) and len(frames) > 1:
        # BUILD 176: explicit refresh (pool reserved): one layer at a time
        _composite_by_layer(self, frames, snapshot, dark, bufs)
        out = sink(self, bufs)
        _refresh_trace.append("encoded %d (free %d MB)" % (len(bufs), _free_mb()))
        return out
    # 1) decode
    pre = self._prefetch(frames, snapshot)
    # 2) composite all N on the card (prefetched blends are stream
    # ordered, no host sync between frames)
    for k, fr in enumerate(frames):
        self._build_dst(fr, dark, bufs[k], snapshot, False, pre, sync=False)
    _check(cudart.cudaStreamSynchronize(self.stream), sync_name)
    # 3) sink: encode / copy out from the device buffers
    out = sink(self, bufs)
    del pre
    return out


def _composite_by_layer(self, frames, snapshot, dark, bufs):
    # BUILD 176: explicit refresh: clear all N buffers, then per track,
    # bottom first: decode that track's N frames, blend them into the N
    # buffers, sync, drop the decoded frames before the next track. Each
    # frame still gets its layers blended bottom first (same image as
    # composite_batch); peak decoded frames held: one layer's N.
    # BUILD 179: a SimpleDecoder keeps its own output frames, as many as
    # its largest get_batch_frames (N), until it is closed; dropping the
    # returned frames frees nothing. So before each track decodes, every
    # decoder of another clip is closed: one layer's decoders hold memory
    # at a time (was: up to DEC_MAX decoders, each with an N-frame pool,
    # building up as the refresh walked the sequence). Stills not in this
    # batch are freed after it (was: every still of the sequence kept).
    # BUILD 180: _refresh_trace gets free VRAM at each step of the batch
    # (console: WORKER refresh vram)
    cudart = _libs()[0]
    del _refresh_trace[:]
    _refresh_trace.append("start free %d MB, decoders open %d" % (
        _free_mb(), len(self.decoders)))
    for b in bufs:
        _check(cudart.cudaMemsetAsync(b, 0 if dark else 255,
                                      self.row * self.seq_h, self.stream), "clear")
    used = set()
    stills = set()
    for fr in frames:
        for item in self._layer_items(fr, snapshot):
            used.add(item[6][0])
            if item[0] == "image":
                stills.add(item[1])
    clips_by_track = snapshot[1]
    for track_id, _en, _solo, _kind in reversed(snapshot[0]):
        clips = set(c[0] for c in clips_by_track.get(track_id, [])) & used
        if not clips:
            continue
        closed = 0
        for key in list(self.decoders.keys()):
            if key[0] not in clips:
                self._close_decoder(key)
                closed += 1
        f_closed = _free_mb()
        before = set(self.decoders.keys())
        pre = self._prefetch(frames, snapshot, clips)
        # BUILD 183: the files (footage or proxy) this track decoded
        files = ", ".join(sorted(set(k[1] for k in self.decoders if k[0] in clips)))
        vram_fatal_check("track %s decode, frames %d..%d, files %s" % (
            track_id, frames[0], frames[-1], files or "none"))
        for k, fr in enumerate(frames):
            self._build_dst(fr, dark, bufs[k], snapshot, False, pre, sync=False,
                            clips=clips, clear=False)
        _check(cudart.cudaStreamSynchronize(self.stream), "composite sync")
        vram_fatal_check("track %s blend, frames %d..%d, files %s" % (
            track_id, frames[0], frames[-1], files or "none"))
        _refresh_trace.append(
            "track %s: closed %d decoders (free %d MB), opened %d, %d frames "
            "batch-decoded from %s, blended (free %d MB)" % (
                track_id, closed, f_closed,
                len(set(self.decoders.keys()) - before), len(pre),
                files or "none", _free_mb()))
        pre.clear()
        del pre
    freed = 0
    for path in list(self.images.keys()):
        if path not in stills:
            item = self.images.pop(path)
            if item is not None:
                cudart.cudaFree(item[0])
            freed += 1
    if freed:
        _refresh_trace.append("stills freed %d (free %d MB)" % (freed, _free_mb()))


# BUILD 180: steps of the last refresh batch (free VRAM after each)
_refresh_trace = []

# BUILD 181/182: temporary hard guard: explicit refresh exits the program
# when free VRAM falls under this (a bad allocation must not freeze the
# PC). BUILD 182: checked inside the batch (each decoder open, each
# layer's decode and blend), not only at its end. _vram_base: free VRAM
# after the pool reserve (drift in the message), set by the worker.
REFRESH_VRAM_FATAL_MB = 1500
_vram_base = [None]


def vram_fatal_check(where):
    # Free VRAM under REFRESH_VRAM_FATAL_MB: print the numbers and exit the
    # process at once (os._exit: no cleanup that could touch the GPU again)
    fv = free_vram()
    if fv is None or fv[0] >= (REFRESH_VRAM_FATAL_MB << 20):
        return
    base = _vram_base[0]
    print("ERROR: WORKER refresh VRAM critically low %s: free %d MB of %d MB "
          "(limit %d MB), drift since reserve %s. Exiting FLAVE now." % (
              where, fv[0] >> 20, fv[1] >> 20, REFRESH_VRAM_FATAL_MB,
              ("%+d MB" % ((fv[0] - base) >> 20)) if base is not None else "?"))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(3)


def _free_mb():
    fv = free_vram()
    return (fv[0] >> 20) if fv else -1


def refresh_owned(gen):
    # BUILD 180: what FLAVE itself holds on the card for the refresh
    # (composite buffers, nvJPEG encoder states, stills, open decoders)
    imgs = [i for i in getattr(gen, "images", {}).values() if i is not None]
    bufs = getattr(gen, "gen_bufs", [])
    nvj = getattr(gen, "nvj", None)
    return ("buffers %d (%d MB), nvJPEG states %d, stills %d (%d MB), "
            "decoders open %d" % (
                len(bufs), (len(bufs) * gen.row * gen.seq_h) >> 20,
                len(nvj.states) if nvj is not None else 0, len(imgs),
                sum(i[3] * i[2] for i in imgs) >> 20,
                len(getattr(gen, "decoders", {}))))


def refresh_vram_guard(project_path, sequence_id, floor):
    # BUILD 180: after a refresh batch: free VRAM below floor bytes closes
    # every decoder (their output pools are the only memory the refresh
    # holds outside the reserved pool). Returns (free before, after) MB or
    # None when nothing was done.
    fv = free_vram()
    if fv is None or fv[0] >= floor:
        return None
    gen = _generator_for(project_path, sequence_id)
    if gen is None:
        return None
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        gen._close_all_decoders()
    finally:
        _GEN_LOCK.release()
    return fv[0] >> 20, _free_mb()


def host_rgb(self, bufs):
    # BUILD 142 sink: each device buffer copied to host RGB24 bytes
    cudart = _libs()[0]
    size = self.row * self.seq_h
    out = []
    for b in bufs:
        host = ctypes.create_string_buffer(size)
        _check(cudart.cudaMemcpy(host, b, size, 2), "dst to host")
        out.append(host.raw)
    return out


def _jpeg_sink(quality):
    # BUILD 142 sink of the preview fill: ("jpeg", bytes) from nvJPEG, or
    # ("raw", host RGB) for the Qt fallback (no nvJPEG on this machine)
    def sink(self, bufs):
        lib = _nvjpeg_lib()
        if lib is not None:
            if self.nvj is None:
                self.nvj = _NvJpegEncoder(lib, self.stream)
            jpegs = self.nvj.encode_many(bufs, self.seq_w, self.seq_h,
                                         self.row, quality)
            return [("jpeg", b) for b in jpegs]
        return [("raw", raw) for raw in host_rgb(self, bufs)]
    return sink

def _frame_locked(self, frame, snapshot, worker, quality):
    # Composite under _GEN_LOCK. ("jpeg", bytes) from nvJPEG, or
    # ("raw", RGB host bytes) for the Qt fallback, or (kind, None)
    if self.closed or not self.ready:
        return ("jpeg", None)
    cudart = _libs()[0]
    try:
        if worker:
            self._tm = _new_tm()
            self._build_dst(frame, _dark_mode(), self.gen_dst, snapshot, False)
            src = self.gen_dst
        else:
            if self._dst_frame != frame:
                self._tm = _new_tm()
                self._build_dst(frame, _dark_mode())
            src = self.dst
        lib = _nvjpeg_lib()
        if lib is not None:
            if self.nvj is None:
                self.nvj = _NvJpegEncoder(lib, self.stream)
            out = self.nvj.encode(src, self.seq_w, self.seq_h, self.row, quality)
            return ("jpeg", out)
        size = self.row * self.seq_h
        host = ctypes.create_string_buffer(size)
        _check(cudart.cudaMemcpy(host, src, size, 2), "dst to host")
        return ("raw", host.raw)
    except Exception as e:
        self._target = None
        print(f"Sequence Player frame_jpeg failed: {e}")
        return ("jpeg", None)

def _player_for(project_path, sequence_id):
    # Open, matching Sequence Player for this sequence, or None
    try:
        import sequence_timeline
        timeline = sequence_timeline._open_windows.get(int(sequence_id))
    except Exception:
        return None
    player = getattr(timeline, "_player", None)
    if player is None or player.closed:
        return None
    if os.path.abspath(project_path) != os.path.abspath(timeline._project_db()):
        return None
    return player


def FrameGenerateBatch(project_path, sequence_id, frames, snapshot):
    # Worker only: JPEG bytes for consecutive sequence frames in one card
    # pass (one lock hold, multi-frame decode), list parallel to frames
    # (None entries on failure), or None if no player is open.
    # BUILD 141: with no player open, the headless GPU composite
    # (sequence_preview_gpu); None only when that cannot start either.
    player = _generator_for(project_path, sequence_id)
    if player is None:
        return None
    return player.frames_jpeg_batch([int(f) for f in frames],
                                    preview_jpeg_quality(), snapshot)


def FrameGenerate(project_path, sequence_id, frame_num, snapshot=None, worker=False):
    # Sequence frame as JPEG bytes (quality 90), or None. Uses the open
    # Sequence Player's GPU context and composite; never opens a window,
    # so with no player open for this sequence it returns None.
    # BUILD 141: worker=True with no player open: headless GPU composite.
    # BUILD 145: worker=True always uses the frame engine (_generator_for),
    # player open or not; the GUI path (worker=False) still needs a player.
    if worker:
        gen = _generator_for(project_path, sequence_id)
        if gen is None:
            return None
        return gen.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)
    try:
        import sequence_timeline
        timeline = sequence_timeline._open_windows.get(int(sequence_id))
    except Exception:
        return None
    player = getattr(timeline, "_player", None)
    if player is None or player.closed:
        # GUI path: needs the player (BUILD 145: the worker returned above)
        return None
    if os.path.abspath(project_path) != os.path.abspath(timeline._project_db()):
        return None
    # BUILD 145: the frame engine is not released for the player any more
    # if worker:
    #     _release_headless()
    return player.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)


def _release_headless():
    try:
        from sequence_preview_gpu import release_headless
        release_headless()
    except Exception as e:
        print(f"Preview headless GPU release failed: {e}")


def _generator_for(project_path, sequence_id):
    # Worker thread: the composer for this sequence's preview frames.
    # BUILD 145: the frame engine (one headless composer, its decoders stay
    # warm when a player opens or closes); the open player only when the
    # engine's GPU setup failed; else None.
    # Was (BUILD 141): the open player first (headless released), else the
    # headless composer.
    try:
        from sequence_preview_gpu import headless_for
        engine = headless_for(project_path, sequence_id)
    except Exception as e:
        print(f"Preview headless GPU failed: {e}")
        engine = None
    if engine is not None:
        return engine
    return _player_for(project_path, sequence_id)


# BUILD 171: numbers of the last refresh_batch_frames call (console log)
_last_batch_info = {}


def free_vram():
    # BUILD 171: (free, total) device bytes, or None
    try:
        cudart = _libs()[0]
        free = ctypes.c_size_t(0)
        total = ctypes.c_size_t(0)
        _check(cudart.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)),
               "mem info")
        return free.value, total.value
    except Exception:
        return None


def refresh_batch_frames(project_path, sequence_id, snapshot, low, high,
                         margin=0.25, headroom=0):
    # BUILD 170: worker, explicit refresh: frames per card pass from free
    # VRAM (cudaMemGetInfo), keeping margin x total VRAM free for other
    # programs. Estimated cost per frame: the RGB composite buffer, an
    # nvJPEG encoder state (about 2 x w x h) and, per enabled video track,
    # one decoded surface (NV12 with room for a larger source: 3 x w x h).
    # Buffers / states this composer already holds are reused, so their
    # size counts as free. Clamped to low..high; low on any failure.
    # BUILD 174: headroom bytes also kept free (NVDEC decode surfaces).
    _last_batch_info.clear()
    try:
        gen = _generator_for(project_path, sequence_id)
        if gen is None:
            _last_batch_info["why"] = "no composer"
            return low
        cudart = _libs()[0]
        free = ctypes.c_size_t(0)
        total = ctypes.c_size_t(0)
        _check(cudart.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)),
               "mem info")
        w, h = int(gen.seq_w), int(gen.seq_h)
        buf = int(gen.row) * h + 2 * w * h
        layers = 0
        for r in (snapshot[0] if snapshot else []):
            if r[1] and r[3] == "video":
                layers += 1
        per = buf + max(1, layers) * 3 * w * h
        held = len(getattr(gen, "gen_bufs", [])) * buf
        margin_b = int(margin * total.value)
        usable = free.value + held - margin_b - int(headroom)
        raw = int(usable // per) if usable > 0 else 0
        # BUILD 175: high <= 0: no upper cap (the VRAM fit is N)
        n = max(low, raw if high <= 0 else min(high, raw))
        _last_batch_info.update(
            free=free.value, total=total.value, margin=margin_b, held=held,
            headroom=int(headroom),
            usable=usable, per=per, buf=buf, layers=layers, raw=raw,
            low=low, high=high, n=n,
            why=("cap" if 0 < high < raw else "floor" if raw < low else "vram"))
        return n
    except Exception as e:
        print(f"Refresh batch size failed: {e}")
        _last_batch_info["why"] = "error"
        return low


# BUILD 184: device bytes per decoded frame per source pixel: the RGB
# output frame a SimpleDecoder keeps (3) plus its NV12 decode surface (1.5)
REFRESH_DECODE_BYTES_PX = 4.5
# BUILD 185: per-chunk fit keeps REFRESH_VRAM_FATAL_MB + this free
REFRESH_CHUNK_CUSHION_MB = 256
_src_size = {}      # path -> (w, h) via ffprobe, or None


def _source_size(path):
    if path not in _src_size:
        try:
            from proxy_manager import probe_size
            _src_size[path] = probe_size(path)
        except Exception:
            _src_size[path] = None
    return _src_size[path]


def refresh_plan(project_path, sequence_id, snapshot, total, pool_n):
    # BUILD 186: worker, explicit refresh, once per pass before any chunk:
    # an edit decision list of the sequence. Ranges are cut at every clip
    # edge (any track) and merged while the layers decoded stay the same
    # (same clip / file on every track); gaps are ranges too. Each range
    # gets its own N once, from the real width x height (ffprobe, cached)
    # of the largest video source decoded in it (proxy or original):
    # REFRESH_DECODE_BYTES_PX per pixel per frame, one layer decoding at a
    # time; plus its stills (RGBA, 4 per pixel, once). Budget: free VRAM
    # with every decoder closed - the safety exit limit - a cushion (the
    # composite buffers are the reserved pool, already out of free).
    # N = budget / per frame, 1..pool_n; a range with no video gets pool_n.
    # Chunks never cross a range end (the worker cuts them), so no chunk
    # mixes two ranges' sources. Returns [(start, end, n)], end inclusive.
    gen = _generator_for(project_path, sequence_id)
    if gen is None or total <= 0:
        return []
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        gen._close_all_decoders()
    finally:
        _GEN_LOCK.release()
    fv = free_vram()
    keep = (REFRESH_VRAM_FATAL_MB + REFRESH_CHUNK_CUSHION_MB) << 20
    budget = (fv[0] - keep) if fv else 0
    cuts = set([0, int(total)])
    for clips in snapshot[1].values():
        for clip in clips:
            for f in (int(clip[2]), int(clip[2]) + int(clip[3])):
                if 0 < f < total:
                    cuts.add(f)
    cuts = sorted(cuts)
    ranges = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        items = gen._layer_items(a, snapshot)
        sig = tuple(sorted((it[0], it[6]) for it in items))
        if ranges and ranges[-1][2] == sig:
            ranges[-1][1] = b - 1
        else:
            ranges.append([a, b - 1, sig, items])
    plan = []
    print("WORKER refresh plan: %d ranges, free %s MB, keep %d MB, pool N %d" % (
        len(ranges), (fv[0] >> 20) if fv else "?", keep >> 20, pool_n))
    for a, b, _sig, items in ranges:
        per, still, big_path, big_size = 0, 0, None, None
        for it in items:
            size = _source_size(it[1]) or (int(gen.seq_w), int(gen.seq_h))
            if it[0] == "image":
                still += 4 * size[0] * size[1]
            elif it[0] == "video":
                cost = int(size[0] * size[1] * REFRESH_DECODE_BYTES_PX)
                if cost > per:
                    per, big_path, big_size = cost, it[1], size
        if per <= 0:
            n = pool_n
        elif fv is None:
            n = 1
        else:
            n = max(1, min(pool_n, int((budget - still) // per)))
        plan.append((a, b, n))
        print("WORKER refresh range %d..%d: N %d | largest source %s %s, "
              "%.1f MB/frame decode, stills %d MB" % (
                  a, b, n, ("%dx%d" % big_size) if big_size else "none",
                  big_path or "", per / float(1 << 20), still >> 20))
    return plan


def reserve_refresh_pool(project_path, sequence_id, n):
    # BUILD 173: worker, start of an explicit refresh: allocate the n
    # composite buffers and n nvJPEG encoder states once; every card pass
    # of the refresh reuses them (no trim) until release_refresh_pool.
    # Returns n, or 0 when the composer is missing or allocation failed
    # (whatever was allocated is freed again).
    gen = _generator_for(project_path, sequence_id)
    if gen is None or n <= 0:
        return 0
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        gen._refresh_hold = int(n)
        gen._gen_buffers(int(n))
        lib = _nvjpeg_lib()
        if lib is not None:
            if gen.nvj is None:
                gen.nvj = _NvJpegEncoder(lib, gen.stream)
            gen.nvj.reserve(int(n))
            # BUILD 180: nvJPEG allocates a state's device buffers at its
            # first encode, not at create: encode the N cleared buffers
            # once now, so that memory is taken (and counted in the free
            # after reserve) here instead of growing during the first batch
            bufs = gen.gen_bufs[:int(n)]
            for b in bufs:
                _check(_libs()[0].cudaMemsetAsync(b, 0, gen.row * gen.seq_h,
                                                  gen.stream), "clear")
            gen.nvj.encode_many(bufs, gen.seq_w, gen.seq_h, gen.row,
                                preview_jpeg_quality())
        return int(n)
    except Exception as e:
        print(f"Refresh pool reserve failed: {e}")
        gen._refresh_hold = 0
        try:
            _trim_gen(gen)
        except Exception:
            pass
        return 0
    finally:
        _GEN_LOCK.release()


def refresh_pool_size(project_path, sequence_id):
    # BUILD 173: frames the composer's refresh pool holds (0: none, e.g. the
    # composer was replaced or released since the reserve)
    gen = _generator_for(project_path, sequence_id)
    return int(getattr(gen, "_refresh_hold", 0) or 0) if gen is not None else 0


def release_refresh_pool(project_path, sequence_id):
    # BUILD 173: end of the explicit refresh: free the pool beyond GEN_KEEP
    gen = _generator_for(project_path, sequence_id)
    if gen is None:
        return
    _GEN_LOCK.acquire()
    try:
        gen._refresh_hold = 0
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        _trim_gen(gen)
        # BUILD 179: decoders grown to N-frame output pools by the refresh
        # are closed (reopened on demand at the normal 1-frame size)
        gen._close_all_decoders()
    except Exception as e:
        print(f"Refresh pool release failed: {e}")
    finally:
        _GEN_LOCK.release()


def preview_jpeg_quality():
    # Global previewJPEG_quality, 1..100 (clamped); 90 if missing or bad.
    # Read per frame so a change applies to the next encode.
    try:
        q = int(str(_get_global_setting("previewJPEG_quality", "90")).strip())
    except Exception:
        return 90
    return max(1, min(100, q))


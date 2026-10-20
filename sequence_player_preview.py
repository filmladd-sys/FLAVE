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

def _prefetch(self, frames, snapshot):
    # {((clip_id, path), src_frame): decoded frame} per clip whose source
    # frames run consecutively across the batch (one decoder per clip)
    reqs = {}
    last = {}
    if frames:
        self._prune_decoders(frames[0], snapshot)
    for fr in frames:
        for item in self._layer_items(fr, snapshot):
            kind, path, src = item[0], item[1], item[2]
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
    # Free composite buffers / nvJPEG states beyond GEN_KEEP (lock held)
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
                         margin=0.25):
    # BUILD 170: worker, explicit refresh: frames per card pass from free
    # VRAM (cudaMemGetInfo), keeping margin x total VRAM free for other
    # programs. Estimated cost per frame: the RGB composite buffer, an
    # nvJPEG encoder state (about 2 x w x h) and, per enabled video track,
    # one decoded surface (NV12 with room for a larger source: 3 x w x h).
    # Buffers / states this composer already holds are reused, so their
    # size counts as free. Clamped to low..high; low on any failure.
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
        usable = free.value + held - margin_b
        raw = int(usable // per) if usable > 0 else 0
        n = max(low, min(high, raw if raw > 0 else low))
        _last_batch_info.update(
            free=free.value, total=total.value, margin=margin_b, held=held,
            usable=usable, per=per, buf=buf, layers=layers, raw=raw,
            low=low, high=high, n=n,
            why=("cap" if raw > high else "floor" if raw < low else "vram"))
        return n
    except Exception as e:
        print(f"Refresh batch size failed: {e}")
        _last_batch_info["why"] = "error"
        return low


def preview_jpeg_quality():
    # Global previewJPEG_quality, 1..100 (clamped); 90 if missing or bad.
    # Read per frame so a change applies to the next encode.
    try:
        q = int(str(_get_global_setting("previewJPEG_quality", "90")).strip())
    except Exception:
        return 90
    return max(1, min(100, q))


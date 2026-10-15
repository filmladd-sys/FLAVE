# sequence_player_preview.py
# JPEG preview path for the Sequence Player: one frame and a card batch.
# Methods take the SequenceTimeline player as self and are attached by
# sequence_player.py. ASCII only.

import ctypes
import os
import time

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from sequence_timeline_sql import _get_global_setting
from sequence_player_gpu import (
    _GEN_LOCK, _gui_wants, _worker_acquire, _last_worker_tm,
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
    t_lock = time.perf_counter()
    wtm = None
    try:
        kind, data = self._frame_locked(frame, snapshot, worker, quality)
        if worker:
            # Own copy before the lock is released (GUI resets self._tm)
            wtm = dict(self._tm)
            wtm["t0"] = t_lock
            _last_worker_tm[0] = wtm
    finally:
        _GEN_LOCK.release()
        if not worker:
            _gui_wants[0] -= 1
    if kind == "jpeg" or data is None:
        return data
    # Qt fallback: encode the host RGB copy outside the lock
    try:
        t_j = time.perf_counter()
        img = QImage(data, self.seq_w, self.seq_h, self.row, QImage.Format_RGB888)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if not img.save(buf, "JPG", quality):
            return None
        out = bytes(buf.data())
        if wtm is not None:
            wtm["jpeg"] = time.perf_counter() - t_j
        return out
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
    t_lock = time.perf_counter()
    try:
        items = self._frames_locked(frames, snapshot, quality)
        wtm = dict(self._tm)
        wtm["t0"] = t_lock
        # Per-frame timing for the worker prints; empty if the player
        # was not ready and the locked path returned early
        wtm["frames"] = list(getattr(self, "_frame_tms", []))
        _last_worker_tm[0] = wtm
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
    # {(path, src_frame): decoded frame} for video sources whose source
    # frames run consecutively across the batch
    reqs = {}
    for fr in frames:
        for kind, path, src in self._layers(fr, snapshot):
            if kind == "video" and os.path.isfile(path):
                reqs.setdefault(path, []).append(src)
    pre = {}
    tm = self._tm
    # Timing shares: (path, src) -> [seek, decode share]
    self._pre_tm = {}
    for path, srcs in reqs.items():
        if len(srcs) < 2 or srcs != list(range(srcs[0], srcs[0] + len(srcs))):
            continue
        dec = self._decoder(path)
        t0 = time.perf_counter()
        if self.dec_next.get(path) != srcs[0]:
            dec.seek_to_index(srcs[0])
            tm["seeked"] = 1
        tm["seek"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        try:
            got = dec.get_batch_frames(len(srcs))
        except Exception:
            self.dec_next.pop(path, None)
            raise
        tm["decode"] += time.perf_counter() - t0
        t_dec = time.perf_counter() - t0
        got = list(got or [])
        # seek before the first frame of the run counts on that frame
        t_seek = tm["seek"] - self._pre_seek_base
        self._pre_seek_base = tm["seek"]
        for k, f in enumerate(got):
            pre[(path, srcs[0] + k)] = f
            self._pre_tm[(path, srcs[0] + k)] = [
                t_seek if (k == 0 and t_seek > 0 and tm["seeked"]) else 0.0,
                t_dec / len(got)]
        if got:
            self.dec_next[path] = srcs[0] + len(got)
        else:
            self.dec_next.pop(path, None)
    return pre

def _gen_buffers(self, n):
    # n sequence-size RGB device buffers for one GPU pass (grown, kept)
    cudart = _libs()[0]
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
    if self.closed or not self.ready:
        return None
    cudart = _libs()[0]
    nvcuda = _libs()[1]
    self._tm = _new_tm()
    dark = _dark_mode()
    n = len(frames)
    self._frame_tms = []
    self._pre_tm = {}
    self._pre_seek_base = self._tm["seek"]
    try:
        _check(nvcuda.cuCtxSetCurrent(self.cuda_ctx), "ctx")
        # 1) decode
        t0 = time.perf_counter()
        pre = self._prefetch(frames, snapshot)
        t_decode = time.perf_counter() - t0
        bufs = self._gen_buffers(n)
        # 2) composite all N on the card (prefetched blends are stream
        # ordered, no host sync between frames)
        t0 = time.perf_counter()
        for k, fr in enumerate(frames):
            ft = {"seek": 0.0, "decode": 0.0, "jpeg": 0.0, "seeked": 0}
            for kind, path, src in self._layers(fr, snapshot):
                share = self._pre_tm.get((path, src))
                if share:
                    ft["seek"] += share[0]
                    ft["decode"] += share[1]
                    if share[0] > 0:
                        ft["seeked"] = 1
            b_seek, b_dec = self._tm["seek"], self._tm["decode"]
            b_seeked, self._tm["seeked"] = self._tm["seeked"], 0
            self._build_dst(fr, dark, bufs[k], snapshot, False, pre, sync=False)
            # Layers not prefetched decode per frame inside _build_dst
            if self._tm["seeked"]:
                ft["seek"] += self._tm["seek"] - b_seek
                ft["seeked"] = 1
            ft["decode"] += self._tm["decode"] - b_dec
            self._tm["seeked"] = b_seeked or self._tm["seeked"]
            self._frame_tms.append(ft)
        _check(cudart.cudaStreamSynchronize(self.stream), "composite sync")
        t_comp = time.perf_counter() - t0
        # 3) encode all N from the device buffers
        t0 = time.perf_counter()
        lib = _nvjpeg_lib()
        if lib is not None:
            if self.nvj is None:
                self.nvj = _NvJpegEncoder(lib, self.stream)
            jpegs = self.nvj.encode_many(bufs, self.seq_w, self.seq_h,
                                         self.row, quality)
            items = [("jpeg", b) for b in jpegs]
        else:
            # Qt fallback needs host pixels (no nvJPEG on this machine)
            size = self.row * self.seq_h
            items = []
            for b in bufs:
                host = ctypes.create_string_buffer(size)
                _check(cudart.cudaMemcpy(host, b, size, 2), "dst to host")
                items.append(("raw", host.raw))
        t_jpeg = time.perf_counter() - t0
        self._tm["pass_decode"] = t_decode
        self._tm["pass_composite"] = t_comp
        self._tm["pass_jpeg"] = t_jpeg
        self._tm["jpeg"] = t_jpeg
        for ft in self._frame_tms:
            ft["jpeg"] = t_jpeg / n
            # this frame's share of the pass (decode share + 1/N of the rest)
            ft["share"] = ft["decode"] + ft["seek"] + (t_comp + t_jpeg) / n
        self._target = None
    except Exception as e:
        self._target = None
        print(f"Sequence Player frame_jpeg failed: {e}")
        items = [("jpeg", None)] * n
    return items

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
            t_j = time.perf_counter()
            if self.nvj is None:
                self.nvj = _NvJpegEncoder(lib, self.stream)
            out = self.nvj.encode(src, self.seq_w, self.seq_h, self.row, quality)
            self._tm["jpeg"] = time.perf_counter() - t_j
            return ("jpeg", out)
        size = self.row * self.seq_h
        host = ctypes.create_string_buffer(size)
        t_h = time.perf_counter()
        _check(cudart.cudaMemcpy(host, src, size, 2), "dst to host")
        raw = host.raw
        self._tm["host"] = time.perf_counter() - t_h
        return ("raw", raw)
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
    player = _player_for(project_path, sequence_id)
    if player is None:
        return None
    return player.frames_jpeg_batch([int(f) for f in frames],
                                    preview_jpeg_quality(), snapshot)


def FrameGenerate(project_path, sequence_id, frame_num, snapshot=None, worker=False):
    # Sequence frame as JPEG bytes (quality 90), or None. Uses the open
    # Sequence Player's GPU context and composite; never opens a window,
    # so with no player open for this sequence it returns None.
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
    return player.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)


def preview_jpeg_quality():
    # Global previewJPEG_quality, 1..100 (clamped); 90 if missing or bad.
    # Read per frame so a change applies to the next encode.
    try:
        q = int(str(_get_global_setting("previewJPEG_quality", "90")).strip())
    except Exception:
        return 90
    return max(1, min(100, q))


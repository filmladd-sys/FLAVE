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
    try:
        _check(nvcuda.cuCtxSetCurrent(self.cuda_ctx), "ctx")
        # 1) decode
        pre = self._prefetch(frames, snapshot)
        bufs = self._gen_buffers(n)
        # 2) composite all N on the card (prefetched blends are stream
        # ordered, no host sync between frames)
        for k, fr in enumerate(frames):
            self._build_dst(fr, dark, bufs[k], snapshot, False, pre, sync=False)
        _check(cudart.cudaStreamSynchronize(self.stream), "composite sync")
        # 3) encode all N from the device buffers
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
    try:
        import sequence_timeline
        timeline = sequence_timeline._open_windows.get(int(sequence_id))
    except Exception:
        return None
    player = getattr(timeline, "_player", None)
    if player is None or player.closed:
        # BUILD 141: the worker composites headless when no player is open
        # (the GUI path still needs the player)
        player = _generator_for(project_path, sequence_id) if worker else None
        if player is None:
            return None
        return player.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)
    if os.path.abspath(project_path) != os.path.abspath(timeline._project_db()):
        return None
    if worker:
        _release_headless()
    return player.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)


def _release_headless():
    try:
        from sequence_preview_gpu import release_headless
        release_headless()
    except Exception as e:
        print(f"Preview headless GPU release failed: {e}")


def _generator_for(project_path, sequence_id):
    # BUILD 141, worker thread: the open player for this sequence (its
    # decoders stay warm; the headless composer is freed so one set of
    # decoders runs), else the headless GPU composer, else None
    player = _player_for(project_path, sequence_id)
    if player is not None:
        _release_headless()
        return player
    try:
        from sequence_preview_gpu import headless_for
        return headless_for(project_path, sequence_id)
    except Exception as e:
        print(f"Preview headless GPU failed: {e}")
        return None


def preview_jpeg_quality():
    # Global previewJPEG_quality, 1..100 (clamped); 90 if missing or bad.
    # Read per frame so a change applies to the next encode.
    try:
        q = int(str(_get_global_setting("previewJPEG_quality", "90")).strip())
    except Exception:
        return 90
    return max(1, min(100, q))


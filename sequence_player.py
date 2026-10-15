# sequence_player.py
# Sequence Player for Ladd Video Editor: a still preview of the frame under
# the timeline's scrub. Plain QWindow + QOpenGLContext (no QOpenGLWidget).
# The picture is the preview JPEG for that frame. A miss asks the preview
# worker to build it. The player does not decode. ASCII only.
#
# GPU path follows VidTest_v02.py: cudaGLSetGLDevice on the GL context,
# PyNvVideoCodec SimpleDecoder in device memory (RGB), a PTX kernel that
# scales a layer to the sequence buffer and blends it, then
# cudaMemcpy2DToArray into a registered GL texture and a textured quad.
#
# CUDA, nvJPEG and the blend kernel live in sequence_player_gpu.py.
# The JPEG preview path lives in sequence_player_preview.py.

import ctypes
import os
import time
import sqlite3
import threading

from PySide6.QtCore import Qt, QEvent, QTimer
from PySide6.QtGui import QImage, QWindow, QOpenGLContext

from sequence_timeline_sql import (
    _get_global_setting, _set_global_setting, _load_sequence_name,
    _add_open_player_id, _remove_open_player_id,
)
from sequence_player_gpu import (
    _GEN_LOCK, _gui_wants, _gui_last, _GUI_QUIET, _worker_burst,
    gui_touch, _GuiGen, _worker_acquire, _new_tm, _last_worker_tm,
    _check, _libs, _nvjpeg_lib, _NvJpegEncoder, _NvJpegDecoder, PTX, _dark_mode,
    _player_format,
    GL_TEXTURE_2D, GL_RGBA, GL_RGBA8, GL_RGB, GL_RGB8,
    GL_PIXEL_UNPACK_BUFFER, GL_STREAM_DRAW, GL_UNPACK_ALIGNMENT,
    GL_UNSIGNED_BYTE, GL_LINEAR, GL_CLAMP_TO_EDGE,
    GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
    GL_COLOR_BUFFER_BIT, GL_QUADS,
)
from sequence_player_preview import (
    frame_jpeg, frames_jpeg_batch, _prefetch, _gen_buffers,
    _frames_locked, _frame_locked,
    FrameGenerate, FrameGenerateBatch, preview_jpeg_quality,
)

# The player is waiting on a preview frame. The worker skips its idle yield
# while this is set, so a miss is filled instead of deferred.
_preview_wait = [0]


class PreviewReader(object):
    # One connection and one thread for playback. Reads previewBatchSeconds
    # of JPEGs at a time into a ring. The GUI never opens the file.

    def __init__(self, player):
        self.player = player
        self.conn = None
        self.thread = None
        self.lock = threading.Lock()
        self.ring = {}
        self.playhead = 0
        self.stop = threading.Event()
        self.wake = threading.Event()

    def start(self, frame):
        self.stop_reader()
        from sequence_preview import preview_db_path
        path = preview_db_path(self.player.timeline._project_db(),
                               self.player.sequence_id)
        if not os.path.isfile(path):
            return
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA query_only = ON")
        self.playhead = int(frame)
        self.stop.clear()
        self.wake.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def note(self, frame):
        with self.lock:
            self.playhead = int(frame)
        self.wake.set()

    def get(self, frame):
        with self.lock:
            return self.ring.get(int(frame))

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def stop_reader(self):
        self.stop.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=1.0)
            self.thread = None
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        with self.lock:
            self.ring.clear()

    def _batch_frames(self):
        try:
            seconds = float(_get_global_setting("previewBatchSeconds", "1") or 1)
        except ValueError:
            seconds = 1.0
        fps = float(getattr(self.player.timeline, "fps", 30) or 30)
        return max(1, int(round(seconds * fps)))

    def _run(self):
        while not self.stop.is_set():
            with self.lock:
                head = self.playhead
                have = set(self.ring)
            count = self._batch_frames()
            last = int(self.player.timeline.total_frames()) - 1
            end = min(last, head + count - 1)
            missing = [f for f in range(head, end + 1) if f not in have]
            if not missing or self.conn is None:
                self.wake.wait(0.05)
                self.wake.clear()
                continue
            t0 = time.perf_counter()
            try:
                rows = self.conn.execute(
                    "SELECT frame, jpeg FROM preview WHERE frame BETWEEN ? AND ? "
                    "AND dirty = 0 AND jpeg IS NOT NULL",
                    (missing[0], missing[-1])).fetchall()
            except sqlite3.Error:
                self.wake.wait(0.05)
                self.wake.clear()
                continue
            t_sql = (time.perf_counter() - t0) * 1000.0
            items = [(int(frame), bytes(blob)) for frame, blob in rows if blob]
            decoded = []
            t1 = time.perf_counter()
            try:
                decoded = self.player.decode_play_batch(items)
            except Exception as e:
                print(f"Play decode batch failed: {e}")
            t_dec = (time.perf_counter() - t1) * 1000.0
            print(f"PLAY: batch n={len(items)} decoded={len(decoded)} "
                  f"sql={t_sql:.1f} decode={t_dec:.1f}")
            got = {frame for frame, _buf in decoded}
            with self.lock:
                for frame, buf in decoded:
                    self.ring[frame] = ("gpu", buf)
                for frame, blob in items:
                    if frame not in got and frame not in self.ring:
                        self.ring[frame] = ("cpu", blob)
                for frame, item in list(self.ring.items()):
                    if frame < self.playhead - 2 and item[0] != "gpu":
                        self.ring.pop(frame, None)


def timeline_snapshot(tl):
    # GUI thread only: copy of the track rows and clips the composite reads,
    # so the preview worker never touches Qt widgets.
    rows = []
    for r in range(tl.table.rowCount()):
        item = tl.table.item(r, 0)
        if item is None:
            continue
        kind = (item.data(Qt.UserRole + 4) or "").lower()
        if kind not in ("video", "image"):
            continue
        rows.append((item.data(Qt.UserRole), bool(item.data(Qt.UserRole + 1)),
                     bool(item.data(Qt.UserRole + 2)), kind))
    clips = {}
    for track_id, _en, _solo, _kind in rows:
        clips[track_id] = [list(c) for c in tl.clips_by_track.get(track_id, [])]
    return rows, clips


# ---------------------------------------------------------------- window

class SequencePlayer(QWindow):
    # Still preview of one timeline. The timeline calls request_frame()
    # whenever its scrub, tracks or clips change.
    def __init__(self, timeline):
        super().__init__()
        self.timeline = timeline
        self.sequence_id = timeline.sequence_id
        self.fmt = _player_format()
        self.setSurfaceType(QWindow.OpenGLSurface)
        self.setFormat(self.fmt)
        self.seq_w, self.seq_h = timeline._sequence_size()
        self.row = self.seq_w * 3        # RGB composite row bytes
        name = _load_sequence_name(self.sequence_id)
        self.setTitle(f"Sequence Player: {name}: #{self.sequence_id}")
        self.ctx = None
        self.ready = False
        self.failed = False
        self.closed = False
        self.shown_frame = None
        self._dst_frame = None           # frame currently held in self.dst
        self._tm = _new_tm()
        self.gen_dst = ctypes.c_void_p() # preview worker composite buffer
        self._target = None              # buffer _blend writes (dst or gen_dst)
        self.stream = ctypes.c_void_p()
        self.cuda_ctx = ctypes.c_void_p()
        self.kernel = ctypes.c_void_p()
        self.expand = ctypes.c_void_p()
        self.dst = ctypes.c_void_p()     # sequence-size RGB composite buffer
        self.rgba = ctypes.c_void_p()    # sequence-size RGBA for the texture
        self.img_res = ctypes.c_void_p() # CUDA registration of the RGBA texture
        self.tex = ctypes.c_uint(0)
        self.pbo = ctypes.c_uint(0)      # GL unpack buffer the RGB dst is copied into
        self.res = ctypes.c_void_p()     # CUDA registration of the PBO
        self.nvj = None                  # _NvJpegEncoder, created on first encode
        self.gen_bufs = []               # GPU pass composite buffers (worker)
        self._frame_tms = []             # per-frame timing for the last batch
        self._glb = None                 # GL 1.5 buffer functions (wglGetProcAddress)
        self.decoders = {}               # path -> SimpleDecoder
        self.dec_next = {}               # path -> next index the decoder yields
        self.images = {}                 # path -> (ptr, w, h, stride) or None
        self.paths = {}                  # footage_id -> original_path
        self._restoring = True
        if not self._restore_geometry():
            w = min(self.seq_w, 960)
            self.resize(w, max(1, int(round(w * self.seq_h / float(self.seq_w)))))
        self._restoring = False
        self._waiting = None             # frame we asked the worker for
        self._retry_armed = False
        self._reader = PreviewReader(self)
        self._nvdec = None               # _NvJpegDecoder, playback only
        self._play_bufs = []             # device RGB buffers for decoded play frames
        self._play_free = []             # indexes into _play_bufs not in the ring
        self._play_lock = threading.Lock()

    # ---- geometry, same scheme as the other windows

    def _geometry_key(self):
        return f"sequence_player_{self.sequence_id}_geometry"

    def _restore_geometry(self):
        geo = _get_global_setting(self._geometry_key(), "")
        if not geo:
            return False
        try:
            parts = [int(p) for p in geo.split(",")]
        except ValueError:
            return False
        if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
            return False
        self.setGeometry(parts[0], parts[1], parts[2], parts[3])
        return True

    def _save_geometry(self):
        if self._restoring:
            return
        g = self.geometry()
        _set_global_setting(self._geometry_key(),
                            f"{g.x()},{g.y()},{g.width()},{g.height()}")

    def moveEvent(self, event):
        super().moveEvent(event)
        self._save_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._save_geometry()

    # ---- window events

    def exposeEvent(self, event):
        if not self.isExposed():
            return
        if not self.ready and not self.failed:
            try:
                self._setup()
            except Exception as e:
                self.failed = True
                self.setTitle(self.title() + " (GPU init failed)")
                print(f"Sequence Player GPU init failed: {e}")
        self.request_frame()

    def event(self, event):
        if event.type() == QEvent.Close:
            self._on_close()
        return super().event(event)

    def _on_close(self):
        if self.closed:
            return
        self.closed = True
        self._clear_wait()
        self._reader.stop_reader()
        self._save_geometry()
        self._release_gpu()
        if getattr(self.timeline, "_player", None) is self:
            self.timeline._player = None
        # Forget it in open_players unless the app is quitting (snapshot kept)
        import sequence_timeline
        sid = getattr(self.timeline, "sequence_id", None)
        if sid is not None and not sequence_timeline.app_quitting:
            _remove_open_player_id(sid)

    # ---- GPU setup, as in VidTest_v02.py

    def _setup(self):
        cudart, nvcuda, gl = _libs()
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(self.fmt)
        if not self.ctx.create():
            raise RuntimeError("QOpenGLContext.create failed")
        self.ctx.makeCurrent(self)
        _check(cudart.cudaGLSetGLDevice(0), "cudaGLSetGLDevice")
        _check(cudart.cudaFree(None), "cuda init")
        _check(nvcuda.cuCtxGetCurrent(ctypes.byref(self.cuda_ctx)), "cuCtxGetCurrent")
        _check(cudart.cudaStreamCreate(ctypes.byref(self.stream)), "stream")
        gl.glGenTextures(1, ctypes.byref(self.tex))
        gl.glBindTexture(GL_TEXTURE_2D, self.tex)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, self.seq_w, self.seq_h, 0,
                        GL_RGBA, GL_UNSIGNED_BYTE, None)
        # RGBA so CUDA can register the texture. RGB cannot be a CUDA image.
        _check(cudart.cudaGraphicsGLRegisterImage(ctypes.byref(self.img_res),
                                                   self.tex.value, GL_TEXTURE_2D, 2),
               "register image")
        # PBO kept for the old path; present no longer uses it
        self._glb = self._gl_buffer_funcs(gl)
        gen, bind, data = self._glb[0], self._glb[1], self._glb[2]
        gen(1, ctypes.byref(self.pbo))
        bind(GL_PIXEL_UNPACK_BUFFER, self.pbo.value)
        data(GL_PIXEL_UNPACK_BUFFER, self.row * self.seq_h, None, GL_STREAM_DRAW)
        bind(GL_PIXEL_UNPACK_BUFFER, 0)
        _check(cudart.cudaGraphicsGLRegisterBuffer(ctypes.byref(self.res),
                                                   self.pbo.value, 2), "register pbo")
        _check(cudart.cudaMalloc(ctypes.byref(self.dst), self.row * self.seq_h), "dst malloc")
        _check(cudart.cudaMalloc(ctypes.byref(self.rgba), self.seq_w * self.seq_h * 4),
               "rgba malloc")
        _check(cudart.cudaMalloc(ctypes.byref(self.gen_dst), self.row * self.seq_h),
               "gen dst malloc")
        module = ctypes.c_void_p()
        _check(nvcuda.cuModuleLoadData(ctypes.byref(module), PTX), "module")
        _check(nvcuda.cuModuleGetFunction(ctypes.byref(self.kernel), module,
                                          b"blend_layer"), "kernel")
        _check(nvcuda.cuModuleGetFunction(ctypes.byref(self.expand), module,
                                          b"expand_rgba"), "expand")
        self.ready = True

    def _gl_buffer_funcs(self, gl):
        # glGenBuffers, glBindBuffer, glBufferData, glDeleteBuffers (GL 1.5,
        # not exported by opengl32.dll; context must be current)
        c = ctypes
        proto = c.WINFUNCTYPE
        names = [(b"glGenBuffers", proto(None, c.c_int, c.POINTER(c.c_uint))),
                 (b"glBindBuffer", proto(None, c.c_uint, c.c_uint)),
                 (b"glBufferData", proto(None, c.c_uint, c.c_ssize_t, c.c_void_p,
                                         c.c_uint)),
                 (b"glDeleteBuffers", proto(None, c.c_int, c.POINTER(c.c_uint)))]
        out = []
        for name, ft in names:
            addr = gl.wglGetProcAddress(name)
            if not addr:
                raise RuntimeError("%s not available" % name.decode())
            out.append(ft(addr))
        return out

    def _release_gpu(self):
        # Under the lock so a preview worker frame is never mid-flight
        with _GuiGen():
            self._release_gpu_locked()

    def _release_gpu_locked(self):
        if not self.ready:
            return
        self.ready = False
        try:
            cudart, nvcuda, gl = _libs()
            self.ctx.makeCurrent(self)
            nvcuda.cuCtxSetCurrent(self.cuda_ctx)
            self.decoders.clear()
            self.dec_next.clear()
            for item in self.images.values():
                if item is not None:
                    cudart.cudaFree(item[0])
            self.images.clear()
            if self.nvj is not None:
                self.nvj.destroy()
                self.nvj = None
            if self._nvdec is not None:
                self._nvdec.destroy()
                self._nvdec = None
            for buf in self._play_bufs:
                if buf.value:
                    cudart.cudaFree(buf)
            self._play_bufs = []
            self._play_free = []
            if self.img_res.value:
                cudart.cudaGraphicsUnregisterResource(self.img_res)
            if self.res.value:
                cudart.cudaGraphicsUnregisterResource(self.res)
            if self.pbo.value and self._glb is not None:
                self._glb[3](1, ctypes.byref(self.pbo))
            if self.dst.value:
                cudart.cudaFree(self.dst)
            if self.rgba.value:
                cudart.cudaFree(self.rgba)
            if self.gen_dst.value:
                cudart.cudaFree(self.gen_dst)
            for b in self.gen_bufs:
                if b.value:
                    cudart.cudaFree(b)
            self.gen_bufs = []
            gl.glDeleteTextures(1, ctypes.byref(self.tex))
            self.ctx.doneCurrent()
        except Exception as e:
            print(f"Sequence Player release failed: {e}")

    # ---- what is under the scrub

    def _footage_path(self, footage_id):
        if footage_id in self.paths:
            return self.paths[footage_id]
        path = None
        try:
            conn = sqlite3.connect(self.timeline._project_db())
            cur = conn.cursor()
            cur.execute("SELECT original_path FROM footage WHERE id = ?", (footage_id,))
            row = cur.fetchone()
            conn.close()
            path = row[0] if row and row[0] else None
        except Exception:
            path = None
        self.paths[footage_id] = path
        return path

    def _layers(self, frame, snapshot=None):
        # (kind, path, source_frame) bottom first. Top table row is the top
        # track, drawn last. Video and image tracks only; disabled tracks
        # skipped; if any track is solo, only solo tracks. snapshot is a
        # timeline_snapshot() for the worker; None reads the live timeline.
        if snapshot is None:
            snapshot = timeline_snapshot(self.timeline)
        rows, clips_by_track = snapshot
        any_solo = any(solo for _tid, _en, solo, _k in rows)
        layers = []
        for track_id, enabled, solo, kind in reversed(rows):
            if not enabled or (any_solo and not solo):
                continue
            for clip in clips_by_track.get(track_id, []):
                start, length = clip[2], clip[3]
                if not clip[5] or not (start <= frame < start + length):
                    continue
                path = self._footage_path(clip[1])
                if path:
                    src = (clip[7] + frame - start) if kind == "video" else 0
                    layers.append((kind, path, max(0, int(src))))
                break
        return layers

    # ---- sources

    def _decoder(self, path):
        dec = self.decoders.get(path)
        if dec is None:
            import PyNvVideoCodec as nvc
            dec = nvc.SimpleDecoder(
                path,
                gpu_id=0,
                use_device_memory=True,
                need_scanned_stream_metadata=True,
                output_color_type=nvc.OutputColorType.RGB,
            )
            self.decoders[path] = dec
        return dec

    def _image(self, path):
        # Still uploaded once to device memory as RGBA8888
        if path in self.images:
            return self.images[path]
        cudart = _libs()[0]
        item = None
        img = QImage(path)
        if not img.isNull():
            img = img.convertToFormat(QImage.Format_RGBA8888)
            raw = bytes(img.constBits())[: img.sizeInBytes()]
            ptr = ctypes.c_void_p()
            _check(cudart.cudaMalloc(ctypes.byref(ptr), len(raw)), "image malloc")
            _check(cudart.cudaMemcpy(ptr, raw, len(raw), 1), "image copy")
            item = (ptr, img.width(), img.height(), img.bytesPerLine())
        self.images[path] = item
        return item

    def _blend(self, src_ptr, sw, sh, stride, bpp, sync=True):
        cudart, nvcuda, _gl = _libs()
        src = ctypes.c_uint64(int(src_ptr))
        target = self._target if self._target is not None else self.dst
        dst = ctypes.c_uint64(int(target.value))
        vals = [ctypes.c_uint32(v) for v in (sw, sh, stride, bpp, self.seq_w, self.seq_h)]
        # Centering, no scale: dest offset when the source is smaller,
        # center-crop offset when it is larger; src = dst + (ox, oy)
        dx = (self.seq_w - sw) // 2 if self.seq_w > sw else 0
        dy = (self.seq_h - sh) // 2 if self.seq_h > sh else 0
        cx = (sw - self.seq_w) // 2 if sw > self.seq_w else 0
        cy = (sh - self.seq_h) // 2 if sh > self.seq_h else 0
        vals += [ctypes.c_int32(cx - dx), ctypes.c_int32(cy - dy)]
        ptrs = [src, dst] + vals
        args = (ctypes.c_void_p * len(ptrs))(
            *[ctypes.cast(ctypes.pointer(v), ctypes.c_void_p) for v in ptrs])
        grid = (self.seq_w * self.seq_h + 255) // 256
        _check(nvcuda.cuLaunchKernel(self.kernel, grid, 1, 1, 256, 1, 1, 0,
                                     self.stream, args, None), "launch")
        if sync:
            self._sync()

    def _sync(self):
        cudart = _libs()[0]
        ev = ctypes.c_void_p()
        _check(cudart.cudaEventCreate(ctypes.byref(ev)), "event")
        _check(cudart.cudaEventRecord(ev, self.stream), "record")
        _check(cudart.cudaEventSynchronize(ev), "sync")
        cudart.cudaEventDestroy(ev)

    # ---- one frame: show the preview JPEG, or ask the worker for it

    def start_read_ahead(self, frame):
        # Play is starting. One connection, then batches from this frame.
        self._reader.start(frame)

    def stop_read_ahead(self):
        self._reader.stop_reader()
        self._release_play_bufs()

    def _ensure_decoder(self):
        if self._nvdec is not None:
            return self._nvdec
        lib = _nvjpeg_lib()
        if lib is None or not self.ready:
            return None
        _libs()[1].cuCtxSetCurrent(self.cuda_ctx)
        self._nvdec = _NvJpegDecoder(lib, self.stream)
        return self._nvdec

    def _play_cap(self):
        try:
            seconds = float(_get_global_setting("previewBatchSeconds", "1") or 1)
        except ValueError:
            seconds = 1.0
        fps = float(getattr(self.timeline, "fps", 30) or 30)
        return max(2, int(round(seconds * fps)) * 2)

    def _take_play_buf(self):
        cudart = _libs()[0]
        if self._play_free:
            return self._play_bufs[self._play_free.pop()]
        if len(self._play_bufs) >= self._play_cap():
            return None
        buf = ctypes.c_void_p()
        _check(cudart.cudaMalloc(ctypes.byref(buf), self.row * self.seq_h), "play buf")
        self._play_bufs.append(buf)
        return buf

    def _release_play_bufs(self):
        # Buffers stay allocated for the next play. The ring no longer holds them.
        self._play_free = list(range(len(self._play_bufs)))

    def decode_play_batch(self, items):
        # Reader thread. items is [(frame, jpeg bytes)]. Returns [(frame, dev)].
        # Falls back to nothing if nvJPEG is missing; the reader keeps the bytes.
        dec = self._ensure_decoder()
        if dec is None or not items:
            return []
        nvcuda = _libs()[1]
        with self._play_lock:
            nvcuda.cuCtxSetCurrent(self.cuda_ctx)
            bufs = []
            kept = []
            for frame, jpeg in items:
                buf = self._take_play_buf()
                if buf is None:
                    break
                bufs.append(buf)
                kept.append((frame, jpeg))
            if not kept:
                return []
            try:
                dec.decode_many([jpeg for _f, jpeg in kept], bufs, self.row)
            except Exception as e:
                print(f"nvJPEG decode failed: {e}")
                for buf in bufs:
                    self._play_free.append(self._play_bufs.index(buf))
                return []
        return [(frame, buf) for (frame, _jpeg), buf in zip(kept, bufs)]

    def reclaim_play(self, before):
        # Return device buffers for frames the playhead has passed
        with self._reader.lock:
            old = [f for f in self._reader.ring if f < before]
            ptrs = []
            for frame in old:
                item = self._reader.ring.pop(frame)
                if item[0] == "gpu":
                    ptrs.append(item[1])
        if not ptrs:
            return
        with self._play_lock:
            for ptr in ptrs:
                try:
                    self._play_free.append(self._play_bufs.index(ptr))
                except ValueError:
                    pass

    def _expand(self, src):
        # RGB device buffer to the RGBA present buffer. Alpha is 255.
        nvcuda = _libs()[1]
        sp = ctypes.c_uint64(int(src.value))
        dp = ctypes.c_uint64(int(self.rgba.value))
        count = ctypes.c_uint32(self.seq_w * self.seq_h)
        ptrs = [sp, dp, count]
        args = (ctypes.c_void_p * 3)(
            *[ctypes.cast(ctypes.pointer(v), ctypes.c_void_p) for v in ptrs])
        grid = (self.seq_w * self.seq_h + 255) // 256
        _check(nvcuda.cuLaunchKernel(self.expand, grid, 1, 1, 256, 1, 1, 0,
                                     self.stream, args, None), "expand")
        self._sync()

    def _copy_to_texture(self):
        # One copy: RGBA device buffer into the registered texture
        cudart = _libs()[0]
        res_arr = (ctypes.c_void_p * 1)(self.img_res)
        _check(cudart.cudaGraphicsMapResources(1, res_arr, None), "map image")
        try:
            arr = ctypes.c_void_p()
            _check(cudart.cudaGraphicsSubResourceGetMappedArray(
                ctypes.byref(arr), self.img_res, 0, 0), "mapped array")
            _check(cudart.cudaMemcpy2DToArray(
                arr, 0, 0, self.rgba, self.seq_w * 4,
                self.seq_w * 4, self.seq_h, 3), "to texture")
        finally:
            _check(cudart.cudaGraphicsUnmapResources(1, res_arr, None), "unmap image")

    def _show_device(self, ptr, dark):
        # Decode buffer is RGB. Expand, copy into the texture, draw.
        _cudart, nvcuda, _gl = _libs()
        self.ctx.makeCurrent(self)
        t0 = time.perf_counter()
        with self._play_lock:
            nvcuda.cuCtxSetCurrent(self.cuda_ctx)
            t1 = time.perf_counter()
            self._expand(ptr)
            t2 = time.perf_counter()
            self._copy_to_texture()
            t3 = time.perf_counter()
        self._present(dark, True)
        t4 = time.perf_counter()
        print(f"PLAY: show ctx={(t1-t0)*1000:.1f} expand={(t2-t1)*1000:.1f} "
              f"copy={(t3-t2)*1000:.1f} present={(t4-t3)*1000:.1f} "
              f"total={(t4-t0)*1000:.1f}")

    def request_frame(self, force=True):
        # Display comes only from {sequence_id}_preview.db. A miss signals
        # the preview worker and holds the last picture until that JPEG lands.
        if self.closed or not self.isExposed():
            return
        if not self.ready:
            return
        frame = int(self.timeline.current_frame)
        if not force and frame == self.shown_frame and self._waiting is None:
            return
        dark = _dark_mode()
        item = None
        if self._reader.running():
            self._reader.note(frame)
            item = self._reader.get(frame)
            self.reclaim_play(frame - 2)
        if item is not None and item[0] == "gpu":
            self._clear_wait()
            try:
                self._show_device(item[1], dark)
                self.shown_frame = frame
            except Exception as e:
                print(f"Sequence Player device show failed: {e}")
                self._present(dark, self.shown_frame is not None)
            return
        if item is not None and item[0] == "cpu":
            print(f"PLAY: frame={frame} cpu fallback")
        jpeg = item[1] if item is not None else None
        if jpeg is None:
            if self._reader.running():
                print(f"PLAY: frame={frame} miss")
            try:
                from sequence_preview import FrameRetrieve
                jpeg = FrameRetrieve(self.timeline._project_db(),
                                     self.sequence_id, frame)
            except Exception as e:
                print(f"Sequence Player preview read failed: {e}")
                jpeg = None
        if jpeg is not None:
            self._clear_wait()
            try:
                self._show_jpeg(jpeg, dark)
                self.shown_frame = frame
            except Exception as e:
                print(f"Sequence Player preview show failed: {e}")
                self._present(dark, self.shown_frame is not None)
            return
        # Miss: ask the worker once for this frame, then retry until it lands
        if self._waiting != frame:
            self._set_wait(frame)
            try:
                self.timeline._retarget_preview(force=True)
            except Exception as e:
                print(f"Sequence Player preview ask failed: {e}")
        self._present(dark, self.shown_frame is not None)
        self._arm_retry()

    def _set_wait(self, frame):
        if self._waiting is None:
            _preview_wait[0] += 1
        self._waiting = frame

    def _clear_wait(self):
        if self._waiting is not None:
            _preview_wait[0] = max(0, _preview_wait[0] - 1)
        self._waiting = None

    def _arm_retry(self):
        # One short retry while a frame is missing. Not a playback clock.
        if self._retry_armed or self.closed:
            return
        self._retry_armed = True
        QTimer.singleShot(40, self._retry_preview)

    def _retry_preview(self):
        self._retry_armed = False
        if self.closed or self._waiting is None:
            return
        self.request_frame(force=True)

    def _show_jpeg(self, jpeg, dark):
        # Decode the preview JPEG and upload it to the display texture
        img = QImage()
        if not img.loadFromData(jpeg, "JPG") or img.isNull():
            raise RuntimeError("preview JPEG decode failed")
        img = img.convertToFormat(QImage.Format_RGB888)
        if img.width() != self.seq_w or img.height() != self.seq_h:
            img = img.scaled(self.seq_w, self.seq_h)
        self.ctx.makeCurrent(self)
        gl = _libs()[2]
        img = img.convertToFormat(QImage.Format_RGBA8888)
        raw = bytes(img.constBits())[: img.sizeInBytes()]
        buf = ctypes.create_string_buffer(raw)
        gl.glBindTexture(GL_TEXTURE_2D, self.tex)
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.seq_w, self.seq_h,
                           GL_RGBA, GL_UNSIGNED_BYTE, buf)
        self._present(dark, True)

    def _build_dst(self, frame, dark, target=None, snapshot=None, use_gl=True,
                   prefetched=None, sync=True):
        # Clear, decode and blend every layer into target (default self.dst,
        # sequence size). The worker passes gen_dst, a snapshot, use_gl False.
        cudart, nvcuda, _gl = _libs()
        if use_gl:
            self.ctx.makeCurrent(self)
        _check(nvcuda.cuCtxSetCurrent(self.cuda_ctx), "ctx")
        target = target if target is not None else self.dst
        self._target = target
        if target is self.dst:
            self._dst_frame = None
        _check(cudart.cudaMemsetAsync(target, 0 if dark else 255,
                                      self.row * self.seq_h, self.stream), "clear")
        for kind, path, src_frame in self._layers(frame, snapshot):
            if kind == "image":
                item = self._image(path)
                if item is not None:
                    self._blend(item[0].value, item[1], item[2], item[3], 4)
                continue
            if not os.path.isfile(path):
                continue
            if prefetched is not None and (path, src_frame) in prefetched:
                # Decoded by a multi-frame get_batch_frames: no seek, no decode
                f = prefetched[(path, src_frame)]
                shape = [int(v) for v in f.shape]
                sh, sw = shape[0], shape[1]
                stride = [int(v) for v in f.strides][0] if f.strides else sw * 3
                self._blend(int(f.GetPtrToPlane(0)), sw, sh, stride, 3, sync)
                continue
            dec = self._decoder(path)
            # Seek only when the asked frame is not the next one this decoder
            # yields; a run in order continues decoding (HEVC GOP 15)
            if self.dec_next.get(path) != src_frame:
                dec.seek_to_index(src_frame)
            try:
                batch = dec.get_batch_frames(1)
            except Exception:
                self.dec_next.pop(path, None)
                raise
            if not batch:
                self.dec_next.pop(path, None)
                continue
            self.dec_next[path] = src_frame + 1
            f = batch[0]
            shape = [int(v) for v in f.shape]
            sh, sw = shape[0], shape[1]
            stride = [int(v) for v in f.strides][0] if f.strides else sw * 3
            # The kernel reads the decoder frame directly
            self._blend(int(f.GetPtrToPlane(0)), sw, sh, stride, 3)
        self._sync()
        self._target = None
        if target is self.dst:
            self._dst_frame = frame

    def _composite(self, frame, dark):
        with _GuiGen():
            self._composite_locked(frame, dark)

    def _composite_locked(self, frame, dark):
        self._tm = _new_tm()
        self._build_dst(frame, dark)
        self._expand(self.dst)
        self._copy_to_texture()

    def _present(self, dark, has_frame):
        if self.ctx is None:
            return
        _cudart, _nvcuda, gl = _libs()
        self.ctx.makeCurrent(self)
        ratio = self.devicePixelRatio()
        win_w = max(1, int(self.width() * ratio))
        win_h = max(1, int(self.height() * ratio))
        shade = 0.0 if dark else 1.0
        gl.glViewport(0, 0, win_w, win_h)
        gl.glClearColor(shade, shade, shade, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)
        if has_frame:
            # Whole buffer fit into the window, aspect kept
            scale = min(win_w / float(self.seq_w), win_h / float(self.seq_h))
            vw = max(1, int(self.seq_w * scale))
            vh = max(1, int(self.seq_h * scale))
            gl.glViewport((win_w - vw) // 2, (win_h - vh) // 2, vw, vh)
            gl.glEnable(GL_TEXTURE_2D)
            gl.glBindTexture(GL_TEXTURE_2D, self.tex)
            gl.glBegin(GL_QUADS)
            gl.glTexCoord2f(0, 1)
            gl.glVertex2f(-1, -1)
            gl.glTexCoord2f(1, 1)
            gl.glVertex2f(1, -1)
            gl.glTexCoord2f(1, 0)
            gl.glVertex2f(1, 1)
            gl.glTexCoord2f(0, 0)
            gl.glVertex2f(-1, 1)
            gl.glEnd()
        self.ctx.swapBuffers(self)



# Preview path methods live in sequence_player_preview.py
SequencePlayer.frame_jpeg = frame_jpeg
SequencePlayer.frames_jpeg_batch = frames_jpeg_batch
SequencePlayer._prefetch = _prefetch
SequencePlayer._gen_buffers = _gen_buffers
SequencePlayer._frames_locked = _frames_locked
SequencePlayer._frame_locked = _frame_locked

def open_player_for(timeline):
    # One player per timeline: raise it if open, else create and show
    player = getattr(timeline, "_player", None)
    if player is not None and not player.closed:
        player.show()
        player.raise_()
        player.requestActivate()
        return player
    player = SequencePlayer(timeline)
    timeline._player = player
    player.show()
    sid = getattr(timeline, "sequence_id", None)
    if sid is not None:
        _add_open_player_id(sid)
    return player


def close_player_for(timeline):
    player = getattr(timeline, "_player", None)
    if player is not None and not player.closed:
        player.close()
    timeline._player = None

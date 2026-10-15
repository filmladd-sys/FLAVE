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
            self.player._last_batch = (len(items), len(decoded), t_sql, t_dec)
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
        self._play_conn = None
        self._play_queue = []
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

    def keyPressEvent(self, event):
        # F11 toggles fullscreen. Escape leaves it. Title bar goes away.
        if event.key() == Qt.Key_F11:
            if self.visibility() == QWindow.FullScreen:
                self.showNormal()
            else:
                self.showFullScreen()
            self.requestActivate()
            return
        if event.key() == Qt.Key_Escape and self.visibility() == QWindow.FullScreen:
            self.showNormal()
            return
        super().keyPressEvent(event)

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
        err = cudart.cudaGLSetGLDevice(0)
        if err != 0 and err != 708:
            raise RuntimeError(f"cudaGLSetGLDevice failed: {err}")
        # 708 means primary context already active (other CUDA already ran);
        # continue on the existing context for single-GPU interop.
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

# Preview path methods live in sequence_player_preview.py
SequencePlayer.frame_jpeg = frame_jpeg
SequencePlayer.frames_jpeg_batch = frames_jpeg_batch
SequencePlayer._prefetch = _prefetch
SequencePlayer._gen_buffers = _gen_buffers
SequencePlayer._frames_locked = _frames_locked
SequencePlayer._frame_locked = _frame_locked

def open_player_for(timeline):
    # One player per timeline: raise it if open, else create and show.
    # show() on a fullscreen window brings the title bar back, so keep it.
    player = getattr(timeline, "_player", None)
    if player is not None and not player.closed:
        if player.visibility() == QWindow.FullScreen:
            player.requestActivate()
            return player
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

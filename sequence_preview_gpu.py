# sequence_preview_gpu.py
# BUILD 141: headless GPU composite for the preview worker, so
# {ProjectName}_{sequence_id}_Preview.db fills in the background with no Sequence Player
# open. No window and no GL: the process-wide CUDA primary context
# (cuda_context, the same one an open player attaches to), one stream and
# the blend kernel (created once, kept), the worker composite buffers,
# per-clip decoders (device memory) and nvJPEG. The composite itself is
# the player's own code (methods borrowed below), so every layer is scaled,
# positioned and blended on the GPU exactly as in the player, and proxies
# stay in device memory. Used only on the preview worker thread, under
# _GEN_LOCK; released when a player for the sequence is open (one set of
# decoders / NVDEC sessions), when the worker goes idle on a closed
# timeline, or when it switches sequence. ASCII only.
# BUILD 142: the render uses its own composer of this class (new_composer /
# free_composer): own decoders and buffers, no Sequence Player, no GL.
# BUILD 145: the frame engine. _COMPOSER is the one composer of the preview
# fill whether or not a Sequence Player is open (the player only presents
# and holds no decoders), so opening or closing the player no longer tears
# down and rebuilds the decoders. It is released only when the worker goes
# idle on a closed timeline or switches sequence (not on player open, as
# the BUILD 141 note above said). The render keeps a private composer:
# it decodes the originals (the preview decodes proxies) at full speed, and
# sharing one decoder set would make each side prune the other's decoders
# (_prune_decoders keeps only the snapshot's clips) and seek every batch.

import ctypes
import os
import sqlite3

from sequence_player_gpu import _GEN_LOCK, _libs, _check, _new_tm, PTX

# Created once in the shared context (never destroyed, like the context)
_STATIC = {}            # "stream", "kernel", "expand"
_COMPOSER = [None]      # the one headless composer, or None
_FAILED = {}            # key -> error text: setup failed, not retried


class _ProjectRef(object):
    # Stands in for player.timeline: the borrowed methods only call
    # timeline._project_db() (footage paths). No Qt object on the worker.
    def __init__(self, project):
        self._path = project

    def _project_db(self):
        return self._path


class HeadlessComposer(object):

    def __init__(self, project, sequence_id, seq_w, seq_h):
        self.key = (os.path.abspath(project), int(sequence_id), int(seq_w), int(seq_h))
        self.timeline = _ProjectRef(project)
        self.sequence_id = int(sequence_id)
        self.seq_w, self.seq_h = int(seq_w), int(seq_h)
        self.row = self.seq_w * 3
        self.ready = False
        self.failed = False
        self.closed = False
        self.headless = True
        self._tm = _new_tm()
        self._target = None
        self._dst_frame = None
        self.dst = ctypes.c_void_p()     # unused (no shown frame); worker uses gen_dst
        self.gen_dst = ctypes.c_void_p()
        self.gen_bufs = []
        self.stream = ctypes.c_void_p()
        self.cuda_ctx = ctypes.c_void_p()
        self.kernel = ctypes.c_void_p()
        self.expand = ctypes.c_void_p()
        self.nvj = None
        self.decoders = {}
        self.dec_next = {}
        self.dec_used = {}
        self.dec_last = {}
        self.images = {}
        self.paths = {}

    def setup(self):
        # Caller holds _GEN_LOCK. Attach the shared context on this thread
        # (created here only if launch-time init did not run), no GL.
        from cuda_context import ensure_current, get_context
        cudart, nvcuda, _gl = _libs()
        ensure_current()
        shared = get_context()
        if not shared or not shared.value:
            raise RuntimeError("no shared CUDA context")
        self.cuda_ctx = ctypes.c_void_p(int(shared.value))
        _check(nvcuda.cuCtxSetCurrent(self.cuda_ctx), "cuCtxSetCurrent")
        if "stream" not in _STATIC:
            stream = ctypes.c_void_p()
            _check(cudart.cudaStreamCreate(ctypes.byref(stream)), "stream")
            module = ctypes.c_void_p()
            _check(nvcuda.cuModuleLoadData(ctypes.byref(module), PTX), "module")
            kernel = ctypes.c_void_p()
            expand = ctypes.c_void_p()
            _check(nvcuda.cuModuleGetFunction(ctypes.byref(kernel), module,
                                              b"blend_layer"), "kernel")
            _check(nvcuda.cuModuleGetFunction(ctypes.byref(expand), module,
                                              b"expand_rgba"), "expand")
            _STATIC.update(stream=stream, kernel=kernel, expand=expand)
        self.stream = _STATIC["stream"]
        self.kernel = _STATIC["kernel"]
        self.expand = _STATIC["expand"]
        _check(cudart.cudaMalloc(ctypes.byref(self.gen_dst), self.row * self.seq_h),
               "gen dst malloc")
        self.ready = True

    def release_locked(self):
        # Caller holds _GEN_LOCK. Decoders, images, nvJPEG and buffers freed;
        # the shared context, stream and kernel stay.
        self.ready = False
        self.closed = True
        try:
            cudart, nvcuda, _gl = _libs()
            if self.cuda_ctx.value:
                nvcuda.cuCtxSetCurrent(self.cuda_ctx)
            self._close_all_decoders()
            for item in self.images.values():
                if item is not None:
                    cudart.cudaFree(item[0])
            self.images.clear()
            if self.nvj is not None:
                self.nvj.destroy()
                self.nvj = None
            if self.gen_dst.value:
                cudart.cudaFree(self.gen_dst)
                self.gen_dst = ctypes.c_void_p()
            for b in self.gen_bufs:
                if b.value:
                    cudart.cudaFree(b)
            self.gen_bufs = []
        except Exception as e:
            print("Preview headless GPU release failed: %s" % e)


def _borrow():
    # The player's composite and preview methods (plain functions)
    import sequence_player as sp
    import sequence_player_preview as spp
    P = sp.SequencePlayer
    for name in ("_footage_path", "_layer_items", "_layers", "_close_decoder",
                 "_close_all_decoders", "_prune_decoders", "_decoder", "_image",
                 "_blend", "_sync", "_build_dst"):
        setattr(HeadlessComposer, name, P.__dict__[name])
    for name in ("frame_jpeg", "frames_jpeg_batch", "_prefetch", "_gen_buffers",
                 "_frames_locked", "_frame_locked"):
        setattr(HeadlessComposer, name, getattr(spp, name))


def _sequence_size(project, sequence_id):
    # Same as the timeline's _sequence_size (1920x1080 if unknown)
    try:
        conn = sqlite3.connect(project)
        try:
            row = conn.execute("SELECT width, height FROM sequences WHERE id = ?",
                               (int(sequence_id),)).fetchone()
        finally:
            conn.close()
        if row and row[0] and row[1]:
            return int(row[0]), int(row[1])
    except Exception:
        pass
    return 1920, 1080


def headless_for(project, sequence_id):
    # Worker thread: the headless composer for this sequence (created on
    # first use, replaced when the sequence or its size changes; BUILD 145:
    # kept while a player opens and closes), or None
    # when the GPU setup failed (not retried for that key this session).
    w, h = _sequence_size(project, sequence_id)
    key = (os.path.abspath(project), int(sequence_id), w, h)
    cur = _COMPOSER[0]
    if cur is not None and cur.key == key and cur.ready:
        return cur
    if key in _FAILED:
        return None
    with _GEN_LOCK:
        cur = _COMPOSER[0]
        if cur is not None:
            _COMPOSER[0] = None
            cur.release_locked()
        if "frame_jpeg" not in HeadlessComposer.__dict__:
            _borrow()
        comp = HeadlessComposer(project, sequence_id, w, h)
        try:
            comp.setup()
        except Exception as e:
            comp.failed = True
            comp.release_locked()
            _FAILED[key] = str(e)
            print("Preview worker: no headless GPU composite (%s); "
                  "frames fill while a Sequence Player is open" % e)
            return None
        _COMPOSER[0] = comp
        # print("Preview worker: headless GPU composite %dx%d" % (w, h))
        return comp


def headless_active():
    return _COMPOSER[0] is not None


def release_headless():
    # Worker thread (or app exit): free the headless composer, if any
    if _COMPOSER[0] is None:
        return
    with _GEN_LOCK:
        cur = _COMPOSER[0]
        _COMPOSER[0] = None
        if cur is not None:
            cur.release_locked()


def new_composer(project, sequence_id):
    # BUILD 142: a private headless composer (the render's), not the
    # preview worker's slot: own decoders, images and buffers on the shared
    # context. Raises when the GPU setup fails (nothing left allocated).
    w, h = _sequence_size(project, sequence_id)
    with _GEN_LOCK:
        if "frame_jpeg" not in HeadlessComposer.__dict__:
            _borrow()
        comp = HeadlessComposer(project, sequence_id, w, h)
        try:
            comp.setup()
        except Exception:
            comp.failed = True
            comp.release_locked()
            raise
        return comp


def free_composer(comp):
    # BUILD 142: free a composer from new_composer (any thread)
    if comp is None:
        return
    with _GEN_LOCK:
        if not comp.closed:
            comp.release_locked()

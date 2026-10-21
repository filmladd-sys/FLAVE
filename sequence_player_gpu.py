# sequence_player_gpu.py
# CUDA, OpenGL, nvJPEG, the blend kernel, and the generate lock.
# No window, no timeline. ASCII only.

import ctypes
import os
import threading
import time

from sequence_timeline_sql import _get_global_setting

# ---------------------------------------------------------------- CUDA / GL

_LIBS = None   # (cudart, nvcuda, gl) once loaded

# One NVDEC: every composite (live, playhead cache fill, preview worker) holds
# _GEN_LOCK. The GUI bumps _gui_wants while it waits so the worker yields.
_GEN_LOCK = threading.RLock()
_gui_wants = [0]
# perf_counter of the last GUI composite or timeline action; the worker
# stays off the lock until the GUI has been quiet for _GUI_QUIET[0] seconds
# (set by the preview worker from previewIdleMs). Inside a burst
# (_worker_burst[0] True) only a GUI waiting on the lock holds it back.
_gui_last = [0.0]
_GUI_QUIET = [0.25]
_worker_burst = [False]
# BUILD 170: explicit refresh pass running: the worker takes the lock at
# once (no wait for GUI activity); set by the preview worker
_worker_fast = [False]


def gui_touch():
    _gui_last[0] = time.perf_counter()


class _GuiGen(object):
    # GUI side: take the lock ahead of the worker
    def __enter__(self):
        _gui_wants[0] += 1
        gui_touch()
        _GEN_LOCK.acquire()
        return self

    def __exit__(self, *exc):
        _GEN_LOCK.release()
        _gui_wants[0] -= 1
        return False


def _worker_acquire():
    # Worker side: wait while the GUI wants the lock or was active in the
    # last _GUI_QUIET[0] seconds (the decoder may hold the GIL), then take it
    if _worker_fast[0]:
        _GEN_LOCK.acquire()
        return
    while (_gui_wants[0] > 0
           or (not _worker_burst[0]
               and time.perf_counter() - _gui_last[0] < _GUI_QUIET[0])):
        time.sleep(0.01)
    _GEN_LOCK.acquire()


def _new_tm():
    return {"dec": "none", "size": "none", "open": 0.0, "len": 0.0,
            "seek": 0.0, "decode": 0.0, "copy": 0.0, "blend": 0.0,
            "clear": 0.0, "sync": 0.0, "map": 0.0, "tex": 0.0,
            "unmap": 0.0, "draw": 0.0, "swap": 0.0,
            "host": 0.0, "jpeg": 0.0, "seeked": 0, "t0": 0.0}


# Timing of the last worker frame_jpeg (read by the preview worker print)
_last_worker_tm = [None]


def _check(err, name):
    if err != 0:
        raise RuntimeError("%s failed: %s" % (name, err))


def _cuda_toolkit_dir():
    # BUILD 231: the CUDA toolkit install folder under Program Files
    # (%ProgramW6432% / %ProgramFiles%), "" when neither is set (not Windows)
    base = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles") or ""
    return os.path.join(base, "NVIDIA GPU Computing Toolkit", "CUDA") if base else ""


def _cuda_bin_dirs():
    # CUDA_PATH / CUDA_PATH_V* bin folders, then the toolkit v*\bin folders,
    # each also with an x64 subfolder
    import glob
    cuda_dirs = []
    for key, val in sorted(os.environ.items()):
        if key.upper() == "CUDA_PATH" or key.upper().startswith("CUDA_PATH_V"):
            if val:
                cuda_dirs.append(os.path.join(val, "bin"))
    toolkit = _cuda_toolkit_dir()
    if toolkit:
        cuda_dirs += sorted(glob.glob(os.path.join(toolkit, "v*", "bin")), reverse=True)
    cuda_dirs += [os.path.join(d, "x64") for d in list(cuda_dirs)]
    return cuda_dirs


def _load_nvjpeg():
    # nvjpeg64_*.dll, same search as cudart; None if not found
    import glob
    names = []
    for d in _cuda_bin_dirs():
        for full in sorted(glob.glob(os.path.join(d, "nvjpeg64_*.dll")), reverse=True):
            if full not in names:
                names.append(full)
    names += ["nvjpeg64_12.dll", "nvjpeg64_13.dll", "nvjpeg64_11.dll"]
    for name in names:
        try:
            if os.path.isabs(name):
                d = os.path.dirname(name)
                if hasattr(os, "add_dll_directory") and os.path.isdir(d):
                    os.add_dll_directory(d)
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


class _NvImage(ctypes.Structure):
    # nvjpegImage_t: up to 4 channel pointers and pitches
    _fields_ = [("channel", ctypes.c_void_p * 4), ("pitch", ctypes.c_size_t * 4)]


NVJPEG_INPUT_RGBI = 5   # interleaved RGB
NVJPEG_CSS_420 = 2

_NVJPEG = [None, False]   # (lib or None, searched)


def _nvjpeg_lib():
    # Loaded once; prints one line and returns None when the DLL is missing
    if _NVJPEG[1]:
        return _NVJPEG[0]
    _NVJPEG[1] = True
    c = ctypes
    lib = _load_nvjpeg()
    if lib is None:
        print("nvJPEG not found, using Qt")
        return None
    vp = c.c_void_p
    lib.nvjpegCreateSimple.argtypes = [c.POINTER(vp)]
    lib.nvjpegDestroy.argtypes = [vp]
    lib.nvjpegEncoderStateCreate.argtypes = [vp, c.POINTER(vp), vp]
    lib.nvjpegEncoderStateDestroy.argtypes = [vp]
    lib.nvjpegEncoderParamsCreate.argtypes = [vp, c.POINTER(vp), vp]
    lib.nvjpegEncoderParamsDestroy.argtypes = [vp]
    lib.nvjpegEncoderParamsSetQuality.argtypes = [vp, c.c_int, vp]
    lib.nvjpegEncoderParamsSetSamplingFactors.argtypes = [vp, c.c_int, vp]
    lib.nvjpegEncodeImage.argtypes = [vp, vp, vp, c.POINTER(_NvImage), c.c_int,
                                      c.c_int, c.c_int, vp]
    lib.nvjpegEncodeRetrieveBitstream.argtypes = [vp, vp, c.c_void_p,
                                                  c.POINTER(c.c_size_t), vp]
    lib.nvjpegJpegStateCreate.argtypes = [vp, c.POINTER(vp)]
    lib.nvjpegJpegStateDestroy.argtypes = [vp]
    lib.nvjpegDecode.argtypes = [vp, vp, c.c_void_p, c.c_size_t, c.c_int,
                                 c.POINTER(_NvImage), vp]
    for fn in ("nvjpegCreateSimple", "nvjpegDestroy", "nvjpegEncoderStateCreate",
               "nvjpegEncoderStateDestroy", "nvjpegEncoderParamsCreate",
               "nvjpegEncoderParamsDestroy", "nvjpegEncoderParamsSetQuality",
               "nvjpegEncoderParamsSetSamplingFactors", "nvjpegEncodeImage",
               "nvjpegEncodeRetrieveBitstream", "nvjpegJpegStateCreate",
               "nvjpegJpegStateDestroy", "nvjpegDecode"):
        getattr(lib, fn).restype = c.c_int
    _NVJPEG[0] = lib
    return lib


class _NvJpegEncoder(object):
    # One nvJPEG encoder per player (its CUDA context and stream). Encodes
    # an interleaved RGB device buffer; only the JPEG bytes come back.
    # encode_many uses at most STATES_MAX encoder states, reused group by
    # group, because each state is big once warmed (about 60 MB at 1080p;
    # one state per frame would cost gigabytes at a large refresh N).
    # nvJPEG allocates a state's device memory at its first encode, not
    # at create; the refresh warms its states in reserve_refresh_pool
    # (sequence_player_preview) before it measures free VRAM.
    STATES_MAX = 16
    def __init__(self, lib, stream):
        self.lib = lib
        self.stream = stream
        self.handle = ctypes.c_void_p()
        self.state = ctypes.c_void_p()
        self.params = ctypes.c_void_p()
        self.quality = None
        self.states = []                 # encode_many: up to STATES_MAX
        _check(lib.nvjpegCreateSimple(ctypes.byref(self.handle)), "nvjpeg create")
        _check(lib.nvjpegEncoderStateCreate(self.handle, ctypes.byref(self.state),
                                            stream), "nvjpeg state")
        _check(lib.nvjpegEncoderParamsCreate(self.handle, ctypes.byref(self.params),
                                             stream), "nvjpeg params")
        _check(lib.nvjpegEncoderParamsSetSamplingFactors(self.params, NVJPEG_CSS_420,
                                                         stream), "nvjpeg sampling")

    def encode(self, src, w, h, pitch, quality):
        lib = self.lib
        cudart = _libs()[0]
        if quality != self.quality:
            _check(lib.nvjpegEncoderParamsSetQuality(self.params, int(quality),
                                                     self.stream), "nvjpeg quality")
            self.quality = quality
        img = _NvImage()
        img.channel[0] = int(src.value)
        img.pitch[0] = int(pitch)
        _check(lib.nvjpegEncodeImage(self.handle, self.state, self.params,
                                     ctypes.byref(img), NVJPEG_INPUT_RGBI,
                                     int(w), int(h), self.stream), "nvjpeg encode")
        length = ctypes.c_size_t(0)
        _check(lib.nvjpegEncodeRetrieveBitstream(self.handle, self.state, None,
                                                 ctypes.byref(length), self.stream),
               "nvjpeg length")
        _check(cudart.cudaStreamSynchronize(self.stream), "nvjpeg sync")
        out = ctypes.create_string_buffer(length.value)
        _check(lib.nvjpegEncodeRetrieveBitstream(self.handle, self.state, out,
                                                 ctypes.byref(length), self.stream),
               "nvjpeg bitstream")
        _check(cudart.cudaStreamSynchronize(self.stream), "nvjpeg sync")
        return out.raw[:length.value]

    def encode_many(self, srcs, w, h, pitch, quality):
        # nvJPEG has no batched encode: queue the encodes on the stream
        # (one encoder state per buffer of a group), sync once, then read
        # the bitstreams. Pixels stay on the GPU. Groups of STATES_MAX
        # buffers; the states are reused by every group (one sync each).
        out = []
        for g0 in range(0, len(srcs), self.STATES_MAX):
            out.extend(self._encode_group(srcs[g0:g0 + self.STATES_MAX],
                                          w, h, pitch, quality))
        return out

    def _encode_group(self, srcs, w, h, pitch, quality):
        lib = self.lib
        cudart = _libs()[0]
        if quality != self.quality:
            _check(lib.nvjpegEncoderParamsSetQuality(self.params, int(quality),
                                                     self.stream), "nvjpeg quality")
            self.quality = quality
        while len(self.states) < len(srcs):
            st = ctypes.c_void_p()
            _check(lib.nvjpegEncoderStateCreate(self.handle, ctypes.byref(st),
                                                self.stream), "nvjpeg state")
            self.states.append(st)
        for k, src in enumerate(srcs):
            img = _NvImage()
            img.channel[0] = int(src.value)
            img.pitch[0] = int(pitch)
            _check(lib.nvjpegEncodeImage(self.handle, self.states[k], self.params,
                                         ctypes.byref(img), NVJPEG_INPUT_RGBI,
                                         int(w), int(h), self.stream), "nvjpeg encode")
        _check(cudart.cudaStreamSynchronize(self.stream), "nvjpeg sync")
        lens = []
        for k in range(len(srcs)):
            length = ctypes.c_size_t(0)
            _check(lib.nvjpegEncodeRetrieveBitstream(self.handle, self.states[k], None,
                                                     ctypes.byref(length), self.stream),
                   "nvjpeg length")
            lens.append(length)
        outs = []
        for k in range(len(srcs)):
            out = ctypes.create_string_buffer(lens[k].value)
            _check(lib.nvjpegEncodeRetrieveBitstream(self.handle, self.states[k], out,
                                                     ctypes.byref(lens[k]), self.stream),
                   "nvjpeg bitstream")
            outs.append(out)
        _check(cudart.cudaStreamSynchronize(self.stream), "nvjpeg sync")
        return [outs[k].raw[:lens[k].value] for k in range(len(srcs))]

    def reserve(self, n):
        # Create encoder states up to n, never more than STATES_MAX (the
        # refresh pool). They take their device memory at first encode.
        n = min(int(n), self.STATES_MAX)
        while len(self.states) < n:
            st = ctypes.c_void_p()
            _check(self.lib.nvjpegEncoderStateCreate(self.handle, ctypes.byref(st),
                                                     self.stream), "nvjpeg state")
            self.states.append(st)

    def trim(self, keep):
        # Free encoder states beyond keep (0 frees all of encode_many's)
        while len(self.states) > keep:
            st = self.states.pop()
            if st.value:
                self.lib.nvjpegEncoderStateDestroy(st)

    def destroy(self):
        lib = self.lib
        for st in self.states:
            if st.value:
                lib.nvjpegEncoderStateDestroy(st)
        self.states = []
        if self.params.value:
            lib.nvjpegEncoderParamsDestroy(self.params)
        if self.state.value:
            lib.nvjpegEncoderStateDestroy(self.state)
        if self.handle.value:
            lib.nvjpegDestroy(self.handle)


class _NvJpegDecoder(object):
    # One nvJPEG decoder per player. Decodes interleaved RGB JPEGs into
    # device buffers. decode_many queues the batch on the stream and syncs once.
    def __init__(self, lib, stream):
        self.lib = lib
        self.stream = stream
        self.handle = ctypes.c_void_p()
        self.states = []
        _check(lib.nvjpegCreateSimple(ctypes.byref(self.handle)), "nvjpeg decode create")

    def _state(self, index):
        lib = self.lib
        while len(self.states) <= index:
            state = ctypes.c_void_p()
            _check(lib.nvjpegJpegStateCreate(self.handle, ctypes.byref(state)),
                   "nvjpeg jpeg state")
            self.states.append(state)
        return self.states[index]

    def decode_many(self, jpegs, bufs, pitch):
        # jpegs: host JPEG bytes. bufs: device RGB pointers, one per jpeg.
        # Host buffers stay alive until the sync.
        lib = self.lib
        cudart = _libs()[0]
        hosts = []
        for i, (jpeg, buf) in enumerate(zip(jpegs, bufs)):
            host = ctypes.create_string_buffer(jpeg)
            hosts.append(host)
            img = _NvImage()
            img.channel[0] = int(buf.value)
            img.pitch[0] = int(pitch)
            _check(lib.nvjpegDecode(self.handle, self._state(i),
                                    ctypes.cast(host, ctypes.c_void_p),
                                    len(jpeg), NVJPEG_INPUT_RGBI,
                                    ctypes.byref(img), self.stream),
                   "nvjpeg decode")
        _check(cudart.cudaStreamSynchronize(self.stream), "nvjpeg decode sync")
        return True

    def destroy(self):
        lib = self.lib
        for state in self.states:
            if state:
                lib.nvjpegJpegStateDestroy(state)
        self.states = []
        if self.handle:
            lib.nvjpegDestroy(self.handle)
            self.handle = ctypes.c_void_p()


def _load_cudart():
    # Same search as footage_manager_display._load_cudart (the icon path),
    # plus the toolkit bin folders, loaded by full path when not on PATH.
    import glob
    tried = []
    names = []
    cuda_dirs = []
    for key, val in sorted(os.environ.items()):
        if key.upper() == "CUDA_PATH" or key.upper().startswith("CUDA_PATH_V"):
            if val:
                cuda_dirs.append(os.path.join(val, "bin"))
    toolkit = _cuda_toolkit_dir()
    if toolkit:
        cuda_dirs += sorted(glob.glob(os.path.join(toolkit, "v*", "bin")), reverse=True)
    cuda_dirs += [os.path.join(d, "x64") for d in list(cuda_dirs)]
    for d in cuda_dirs:
        for full in sorted(glob.glob(os.path.join(d, "cudart64_*.dll")), reverse=True):
            if full not in names:
                names.append(full)
    names += ["cudart64_12.dll", "cudart64_13.dll", "cudart64_11.dll",
              "cudart64_120.dll", "cudart64_110.dll"]
    for name in names:
        tried.append(name)
        try:
            if os.path.isabs(name):
                d = os.path.dirname(name)
                if hasattr(os, "add_dll_directory") and os.path.isdir(d):
                    os.add_dll_directory(d)
            return ctypes.CDLL(name)
        except OSError:
            continue
    print("Sequence Player: cudart not found. Tried:")
    for name in tried:
        print("  " + name)
    print("  (searched dirs: %s)" % (", ".join(cuda_dirs) or "none"))
    raise RuntimeError("cudart64 dll not found")


def _libs():
    # Load cudart, nvcuda and opengl32 once and set the ctypes signatures
    # of every call the player, worker and render use
    global _LIBS
    if _LIBS is not None:
        return _LIBS
    c = ctypes
    cudart = _load_cudart()
    cudart.cudaGLSetGLDevice.argtypes = [c.c_int]
    cudart.cudaGLSetGLDevice.restype = c.c_int
    cudart.cudaSetDevice.argtypes = [c.c_int]
    cudart.cudaSetDevice.restype = c.c_int
    cudart.cudaGetLastError.argtypes = []
    cudart.cudaGetLastError.restype = c.c_int
    cudart.cudaMalloc.argtypes = [c.POINTER(c.c_void_p), c.c_size_t]
    cudart.cudaMalloc.restype = c.c_int
    cudart.cudaFree.argtypes = [c.c_void_p]
    cudart.cudaFree.restype = c.c_int
    # BUILD 170: free / total device memory (refresh batch size)
    cudart.cudaMemGetInfo.argtypes = [c.POINTER(c.c_size_t), c.POINTER(c.c_size_t)]
    cudart.cudaMemGetInfo.restype = c.c_int
    cudart.cudaMemcpy.argtypes = [c.c_void_p, c.c_void_p, c.c_size_t, c.c_int]
    cudart.cudaMemcpy.restype = c.c_int
    cudart.cudaMemsetAsync.argtypes = [c.c_void_p, c.c_int, c.c_size_t, c.c_void_p]
    cudart.cudaMemsetAsync.restype = c.c_int
    cudart.cudaMemcpy2DToArray.argtypes = [c.c_void_p, c.c_size_t, c.c_size_t, c.c_void_p,
                                           c.c_size_t, c.c_size_t, c.c_size_t, c.c_int]
    cudart.cudaMemcpy2DToArray.restype = c.c_int
    cudart.cudaGraphicsGLRegisterImage.argtypes = [c.POINTER(c.c_void_p), c.c_uint,
                                                   c.c_uint, c.c_uint]
    cudart.cudaGraphicsGLRegisterImage.restype = c.c_int
    cudart.cudaGraphicsUnregisterResource.argtypes = [c.c_void_p]
    cudart.cudaGraphicsUnregisterResource.restype = c.c_int
    cudart.cudaGraphicsMapResources.argtypes = [c.c_int, c.POINTER(c.c_void_p), c.c_void_p]
    cudart.cudaGraphicsMapResources.restype = c.c_int
    cudart.cudaGraphicsUnmapResources.argtypes = [c.c_int, c.POINTER(c.c_void_p), c.c_void_p]
    cudart.cudaGraphicsUnmapResources.restype = c.c_int
    cudart.cudaGraphicsSubResourceGetMappedArray.argtypes = [c.POINTER(c.c_void_p), c.c_void_p,
                                                             c.c_uint, c.c_uint]
    cudart.cudaGraphicsSubResourceGetMappedArray.restype = c.c_int
    cudart.cudaEventCreate.argtypes = [c.POINTER(c.c_void_p)]
    cudart.cudaEventCreate.restype = c.c_int
    cudart.cudaEventDestroy.argtypes = [c.c_void_p]
    cudart.cudaEventDestroy.restype = c.c_int
    cudart.cudaEventRecord.argtypes = [c.c_void_p, c.c_void_p]
    cudart.cudaEventRecord.restype = c.c_int
    cudart.cudaEventSynchronize.argtypes = [c.c_void_p]
    cudart.cudaEventSynchronize.restype = c.c_int
    cudart.cudaStreamCreate.argtypes = [c.POINTER(c.c_void_p)]
    cudart.cudaStreamCreate.restype = c.c_int
    cudart.cudaStreamSynchronize.argtypes = [c.c_void_p]
    cudart.cudaStreamSynchronize.restype = c.c_int
    cudart.cudaGraphicsGLRegisterBuffer.argtypes = [c.POINTER(c.c_void_p), c.c_uint,
                                                    c.c_uint]
    cudart.cudaGraphicsGLRegisterBuffer.restype = c.c_int
    cudart.cudaGraphicsResourceGetMappedPointer.argtypes = [
        c.POINTER(c.c_void_p), c.POINTER(c.c_size_t), c.c_void_p]
    cudart.cudaGraphicsResourceGetMappedPointer.restype = c.c_int

    nvcuda = c.WinDLL("nvcuda.dll")
    nvcuda.cuInit.argtypes = [c.c_uint]
    nvcuda.cuInit.restype = c.c_int
    nvcuda.cuCtxGetCurrent.argtypes = [c.POINTER(c.c_void_p)]
    nvcuda.cuCtxGetCurrent.restype = c.c_int
    nvcuda.cuCtxSetCurrent.argtypes = [c.c_void_p]
    nvcuda.cuCtxSetCurrent.restype = c.c_int
    nvcuda.cuModuleLoadData.argtypes = [c.POINTER(c.c_void_p), c.c_char_p]
    nvcuda.cuModuleLoadData.restype = c.c_int
    nvcuda.cuModuleGetFunction.argtypes = [c.POINTER(c.c_void_p), c.c_void_p, c.c_char_p]
    nvcuda.cuModuleGetFunction.restype = c.c_int
    nvcuda.cuLaunchKernel.argtypes = [
        c.c_void_p,
        c.c_uint, c.c_uint, c.c_uint,
        c.c_uint, c.c_uint, c.c_uint,
        c.c_uint, c.c_void_p,
        c.POINTER(c.c_void_p), c.c_void_p,
    ]
    nvcuda.cuLaunchKernel.restype = c.c_int
    _check(nvcuda.cuInit(0), "cuInit")

    gl = c.WinDLL("opengl32.dll")
    gl.glGenTextures.argtypes = [c.c_int, c.POINTER(c.c_uint)]
    gl.glDeleteTextures.argtypes = [c.c_int, c.POINTER(c.c_uint)]
    gl.glBindTexture.argtypes = [c.c_uint, c.c_uint]
    gl.glTexParameteri.argtypes = [c.c_uint, c.c_uint, c.c_int]
    gl.glTexImage2D.argtypes = [c.c_uint, c.c_int, c.c_int, c.c_int, c.c_int, c.c_int,
                                c.c_uint, c.c_uint, c.c_void_p]
    gl.glEnable.argtypes = [c.c_uint]
    gl.glBegin.argtypes = [c.c_uint]
    gl.glEnd.argtypes = []
    gl.glTexCoord2f.argtypes = [c.c_float, c.c_float]
    gl.glVertex2f.argtypes = [c.c_float, c.c_float]
    gl.glClear.argtypes = [c.c_uint]
    gl.glClearColor.argtypes = [c.c_float, c.c_float, c.c_float, c.c_float]
    gl.glViewport.argtypes = [c.c_int, c.c_int, c.c_int, c.c_int]
    gl.glPixelStorei.argtypes = [c.c_uint, c.c_int]
    gl.glTexSubImage2D.argtypes = [c.c_uint, c.c_int, c.c_int, c.c_int, c.c_int,
                                   c.c_int, c.c_uint, c.c_uint, c.c_void_p]
    gl.wglGetProcAddress.argtypes = [c.c_char_p]
    gl.wglGetProcAddress.restype = c.c_void_p
    _LIBS = (cudart, nvcuda, gl)
    return _LIBS


GL_TEXTURE_2D = 0x0DE1
GL_RGBA = 0x1908
GL_RGBA8 = 0x8058
GL_RGB = 0x1907
GL_RGB8 = 0x8051
GL_PIXEL_UNPACK_BUFFER = 0x88EC
GL_STREAM_DRAW = 0x88E0
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_UNSIGNED_BYTE = 0x1401
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_COLOR_BUFFER_BIT = 0x4000
GL_QUADS = 0x0007

# One layer: src (sw x sh, stride, 3 or 4 bytes per pixel) copied 1:1, no
# scaling, centered in the dw x dh buffer and alpha-blended onto dst (RGB8,
# 3 bytes per pixel; overlay alpha is consumed, not stored).
# With inv_scale=1 and pos_dx=pos_dy=0: source = dest + (ox, oy).
# inv_scale=1/scale scales about the dest center; pos_dx/pos_dy are free
# placement shifts (nearest). 3 bpp opaque; 4 uses source alpha.
# inv_scale is x; inv_scale_y is y (they differ slightly for a proxy whose
# even-rounded size is not an exact fraction of the original).
# opacity (0..1) multiplies the layer alpha (opacity keyframes).
# Sampling is at pixel centers (x + 0.5 in, - 0.5 out): identical at
# inv_scale 1, and a scaled proxy frames to the same pixels as the original.
# BUILD 219: rot_cos / rot_sin (cos and sin of the rotate keyframe angle;
# 1, 0 = no rotation). The layer turns about its center on screen (the
# dest center plus pos_dx / pos_dy); a positive angle turns the picture
# clockwise (y down). Per dest pixel the offset from that center
# (dx, dy) is turned back first, u = cos * dx + sin * dy and
# v = -sin * dx + cos * dy, then scaled by inv_scale / inv_scale_y and
# mapped to the source as before, so scale and the proxy factors stay in
# source axes and crop_x / crop_y crop the source before it turns. Dest
# pixels whose sample falls outside the source (or the crop) are not
# drawn, so the corners a turn uncovers keep the destination as it is.
# u starts as dx * cos (mul.rn) and adds dy * sin (fma.rn); v likewise
# from dy * cos and dx * -sin. At cos 1, sin 0 both are exact (x * 1 = x,
# x + y * 0 = x), so no rotation is bit-exact with the unrotated kernel.
# BUILD 220: outline_w / outline_rgba (border outline keyframes; width in
# sequence pixels, 0 = off; color r | g << 8 | b << 16 | a << 24). A dest
# pixel the layer does not draw (its sample is outside the source or the
# crop) goes to OUTLINE instead of DONE. With width 0 it ends there
# (bit-exact with no outline). Otherwise the visible source rect is the
# pixel range the bounds and crop tests accept, columns
# max(0, ceil(crop_x - 0.5)) .. min(sw - 1, floor(sw - crop_x - 0.5)),
# whose edges in the pre-round sample coordinate fx are those - 0.5 / +
# 0.5 (rows the same with crop_y, sh); an empty rect draws nothing. The
# distance of fx (fy) outside that range, divided by inv_scale
# (inv_scale_y), is the distance in sequence pixels along the clip's own
# rotated axes. The pixel is outline when both distances are < width (a
# band width pixels thick around the outside, square corners, turning
# with the clip); it then blends the outline RGB at alpha outline_a *
# opacity through the same COLOR_DONE math (color keyframes do not apply
# to it). Pixels the layer draws are unchanged.
# BUILD 154: color_flags / contrast / brightness / inv_gamma: per source
# RGB channel c = v / 255, in this order, each step only when its flag bit
# is set (identity = not set): 1 contrast (c - 0.5) * contrast + 0.5,
# clamp 0..1; 2 brightness c + brightness (-1..1), clamp 0..1; 4 gamma
# c ** inv_gamma (inv_gamma = 1 / gamma). Back to round(c * 255). Alpha and
# the opacity blend after it are unchanged; flags 0 skips it all.
# BUILD 152: crop_y is the same real crop on y: drawn only if the source
# pixel center lies in [crop_y, sh - crop_y] (top and bottom; picture in
# place; <= 0 = no crop; >= sh / 2 hides the layer). The BUILD 147 y shift
# below no longer applies either.
# BUILD 151: crop_x is a real crop: a source pixel is drawn only if its
# center lies in [crop_x, sw - crop_x] (crop_x source pixels removed from
# the left and from the right; the picture does not move; crop_x <= 0 = no
# crop; crop_x >= sw / 2 hides the layer). crop_y keeps the BUILD 147 rule
# below. The BUILD 147 note (crop_x part no longer applies):
# BUILD 147: crop_x / crop_y (source pixels, f32; 0 = unchanged). The layer
# box is tested at the uncropped source position (sx, sy) as before; the
# pixel is then read at (sx + crop_x, sy + crop_y), and a read outside the
# source leaves the destination as it is (cropped away). Positive crop_x
# shows source further right (the picture moves left inside its box, the
# box's right strip empties); negative the other way. Same for crop_y.
PTX = b"""
.version 7.0
.target sm_75
.address_size 64
.visible .entry blend_layer(
    .param .u64 src, .param .u64 dst,
    .param .u32 sw, .param .u32 sh, .param .u32 sstride, .param .u32 bpp,
    .param .u32 dw, .param .u32 dh, .param .s32 ox, .param .s32 oy,
    .param .f32 inv_scale, .param .s32 pos_dx, .param .s32 pos_dy,
    .param .f32 inv_scale_y, .param .f32 opacity,
    .param .f32 crop_x, .param .f32 crop_y,
    .param .u32 color_flags, .param .f32 contrast, .param .f32 brightness,
    .param .f32 inv_gamma, .param .f32 rot_cos, .param .f32 rot_sin,
    .param .f32 outline_w, .param .u32 outline_rgba)
{
    .reg .pred p;
    .reg .u32 idx, x, y, dwv, dhv, swv, shv, ssv, bppv, pix;
    .reg .u32 usx, usy, off32, r, g, b, a, dr, dg, db, inv, c255;
    .reg .s32 oxv, oyv, sx, sy, pdx, pdy;
    .reg .f32 invs, invsy, fx, fy, fcx, fcy, fox, foy, fdx, fdy, fop, fa;
    .reg .f32 fcrx, fcry, fsx, fsw, fsy, fsh;
    .reg .pred pc, pb, pg;
    .reg .u32 cflag, cfl;
    .reg .f32 fcon, fbri, figm, fcc;
    .reg .f32 frc, frs, frns, fru, frv;
    .reg .f32 fow, fol, fohi, foe, fod, fodb;
    .reg .u32 orgba;
    .reg .u64 srcv, dstv, addr;
    mov.u32 idx, %tid.x;
    mov.u32 x, %ctaid.x;
    mul.lo.u32 x, x, 256;
    add.u32 idx, idx, x;
    ld.param.u32 dwv, [dw];
    ld.param.u32 dhv, [dh];
    mul.lo.u32 pix, dwv, dhv;
    setp.ge.u32 p, idx, pix;
    @p bra DONE;
    div.u32 y, idx, dwv;
    mul.lo.u32 x, y, dwv;
    sub.u32 x, idx, x;
    ld.param.u32 swv, [sw];
    ld.param.u32 shv, [sh];
    ld.param.u32 ssv, [sstride];
    ld.param.u32 bppv, [bpp];
    ld.param.s32 oxv, [ox];
    ld.param.s32 oyv, [oy];
    ld.param.f32 invs, [inv_scale];
    ld.param.f32 invsy, [inv_scale_y];
    ld.param.s32 pdx, [pos_dx];
    ld.param.s32 pdy, [pos_dy];
    cvt.rn.f32.u32 fx, x;
    cvt.rn.f32.u32 fy, y;
    add.f32 fx, fx, 0f3F000000;
    add.f32 fy, fy, 0f3F000000;
    cvt.rn.f32.u32 fcx, dwv;
    cvt.rn.f32.u32 fcy, dhv;
    mul.f32 fcx, fcx, 0f3F000000;
    mul.f32 fcy, fcy, 0f3F000000;
    cvt.rn.f32.s32 fdx, pdx;
    cvt.rn.f32.s32 fdy, pdy;
    sub.f32 fx, fx, fcx;
    sub.f32 fx, fx, fdx;
    sub.f32 fy, fy, fcy;
    sub.f32 fy, fy, fdy;
    // BUILD 219: inverse rotation of (dx, dy) about the layer center,
    // u = c * dx + s * dy, v = -s * dx + c * dy (exact at c 1, s 0)
    ld.param.f32 frc, [rot_cos];
    ld.param.f32 frs, [rot_sin];
    neg.f32 frns, frs;
    mul.rn.f32 fru, fx, frc;
    fma.rn.f32 fru, fy, frs, fru;
    mul.rn.f32 frv, fy, frc;
    fma.rn.f32 frv, fx, frns, frv;
    mov.f32 fx, fru;
    mov.f32 fy, frv;
    mul.f32 fx, fx, invs;
    add.f32 fx, fx, fcx;
    mul.f32 fy, fy, invsy;
    add.f32 fy, fy, fcy;
    cvt.rn.f32.s32 fox, oxv;
    cvt.rn.f32.s32 foy, oyv;
    add.f32 fx, fx, fox;
    add.f32 fy, fy, foy;
    sub.f32 fx, fx, 0f3F000000;
    sub.f32 fy, fy, 0f3F000000;
    cvt.rni.s32.f32 sx, fx;
    cvt.rni.s32.f32 sy, fy;
    setp.lt.s32 p, sx, 0;
    @p bra OUTLINE;
    setp.lt.s32 p, sy, 0;
    @p bra OUTLINE;
    setp.ge.s32 p, sx, swv;
    @p bra OUTLINE;
    setp.ge.s32 p, sy, shv;
    @p bra OUTLINE;
    ld.param.f32 fcrx, [crop_x];
    ld.param.f32 fcry, [crop_y];
    // BUILD 151: crop_x removes fcrx source px from the left and from the
    // right of the source (the picture stays in place); was a shift:
    // add.f32 fx, fx, fcrx;
    cvt.rn.f32.s32 fsx, sx;
    add.f32 fsx, fsx, 0f3F000000;
    setp.lt.f32 p, fsx, fcrx;
    @p bra OUTLINE;
    cvt.rn.f32.u32 fsw, swv;
    sub.f32 fsw, fsw, fcrx;
    setp.gt.f32 p, fsx, fsw;
    @p bra OUTLINE;
    // BUILD 152: crop_y the same way, top and bottom (was a shift):
    // add.f32 fy, fy, fcry;
    cvt.rn.f32.s32 fsy, sy;
    add.f32 fsy, fsy, 0f3F000000;
    setp.lt.f32 p, fsy, fcry;
    @p bra OUTLINE;
    cvt.rn.f32.u32 fsh, shv;
    sub.f32 fsh, fsh, fcry;
    setp.gt.f32 p, fsy, fsh;
    @p bra OUTLINE;
    cvt.rni.s32.f32 sx, fx;
    cvt.rni.s32.f32 sy, fy;
    setp.lt.s32 p, sx, 0;
    @p bra OUTLINE;
    setp.lt.s32 p, sy, 0;
    @p bra OUTLINE;
    setp.ge.s32 p, sx, swv;
    @p bra OUTLINE;
    setp.ge.s32 p, sy, shv;
    @p bra OUTLINE;
    cvt.u32.s32 usx, sx;
    cvt.u32.s32 usy, sy;
    mul.lo.u32 off32, usy, ssv;
    mad.lo.u32 off32, usx, bppv, off32;
    cvt.u64.u32 addr, off32;
    ld.param.u64 srcv, [src];
    add.u64 addr, srcv, addr;
    ld.global.u8 r, [addr];
    ld.global.u8 g, [addr+1];
    ld.global.u8 b, [addr+2];
    mov.u32 a, 255;
    setp.ne.u32 p, bppv, 4;
    @p bra BLEND;
    ld.global.u8 a, [addr+3];
BLEND:
    // BUILD 154: color keyframes on the source RGB (not alpha), before the
    // opacity / alpha blend. flags 0 (all identity): skipped, bit-exact.
    // c = channel / 255; bit 1: contrast (c - 0.5) * contrast + 0.5, clamp
    // 0..1; bit 2: brightness c + brightness (-1..1), clamp 0..1; bit 4:
    // gamma c ** inv_gamma (ex2(lg2(c) * inv_gamma); c = 0 stays 0); then
    // round(c * 255) <= 255.
    ld.param.u32 cflag, [color_flags];
    setp.eq.u32 p, cflag, 0;
    @p bra COLOR_DONE;
    ld.param.f32 fcon, [contrast];
    ld.param.f32 fbri, [brightness];
    ld.param.f32 figm, [inv_gamma];
    and.b32 cfl, cflag, 1;
    setp.ne.u32 pc, cfl, 0;
    and.b32 cfl, cflag, 2;
    setp.ne.u32 pb, cfl, 0;
    and.b32 cfl, cflag, 4;
    setp.ne.u32 pg, cfl, 0;
    cvt.rn.f32.u32 fcc, r;
    div.rn.f32 fcc, fcc, 0f437F0000;
    @pc sub.rn.f32 fcc, fcc, 0f3F000000;
    @pc mul.rn.f32 fcc, fcc, fcon;
    @pc add.rn.f32 fcc, fcc, 0f3F000000;
    @pc max.f32 fcc, fcc, 0f00000000;
    @pc min.f32 fcc, fcc, 0f3F800000;
    @pb add.rn.f32 fcc, fcc, fbri;
    @pb max.f32 fcc, fcc, 0f00000000;
    @pb min.f32 fcc, fcc, 0f3F800000;
    @pg lg2.approx.f32 fcc, fcc;
    @pg mul.rn.f32 fcc, fcc, figm;
    @pg ex2.approx.f32 fcc, fcc;
    mul.rn.f32 fcc, fcc, 0f437F0000;
    cvt.rni.u32.f32 r, fcc;
    min.u32 r, r, 255;
    cvt.rn.f32.u32 fcc, g;
    div.rn.f32 fcc, fcc, 0f437F0000;
    @pc sub.rn.f32 fcc, fcc, 0f3F000000;
    @pc mul.rn.f32 fcc, fcc, fcon;
    @pc add.rn.f32 fcc, fcc, 0f3F000000;
    @pc max.f32 fcc, fcc, 0f00000000;
    @pc min.f32 fcc, fcc, 0f3F800000;
    @pb add.rn.f32 fcc, fcc, fbri;
    @pb max.f32 fcc, fcc, 0f00000000;
    @pb min.f32 fcc, fcc, 0f3F800000;
    @pg lg2.approx.f32 fcc, fcc;
    @pg mul.rn.f32 fcc, fcc, figm;
    @pg ex2.approx.f32 fcc, fcc;
    mul.rn.f32 fcc, fcc, 0f437F0000;
    cvt.rni.u32.f32 g, fcc;
    min.u32 g, g, 255;
    cvt.rn.f32.u32 fcc, b;
    div.rn.f32 fcc, fcc, 0f437F0000;
    @pc sub.rn.f32 fcc, fcc, 0f3F000000;
    @pc mul.rn.f32 fcc, fcc, fcon;
    @pc add.rn.f32 fcc, fcc, 0f3F000000;
    @pc max.f32 fcc, fcc, 0f00000000;
    @pc min.f32 fcc, fcc, 0f3F800000;
    @pb add.rn.f32 fcc, fcc, fbri;
    @pb max.f32 fcc, fcc, 0f00000000;
    @pb min.f32 fcc, fcc, 0f3F800000;
    @pg lg2.approx.f32 fcc, fcc;
    @pg mul.rn.f32 fcc, fcc, figm;
    @pg ex2.approx.f32 fcc, fcc;
    mul.rn.f32 fcc, fcc, 0f437F0000;
    cvt.rni.u32.f32 b, fcc;
    min.u32 b, b, 255;
COLOR_DONE:
    ld.param.f32 fop, [opacity];
    cvt.rn.f32.u32 fa, a;
    mul.f32 fa, fa, fop;
    cvt.rni.u32.f32 a, fa;
    min.u32 a, a, 255;
    mov.u32 c255, 255;
    sub.u32 inv, c255, a;
    mul.lo.u32 off32, idx, 3;
    cvt.u64.u32 addr, off32;
    ld.param.u64 dstv, [dst];
    add.u64 addr, dstv, addr;
    ld.global.u8 dr, [addr];
    ld.global.u8 dg, [addr+1];
    ld.global.u8 db, [addr+2];
    mul.lo.u32 r, r, a;
    mad.lo.u32 r, dr, inv, r;
    div.u32 r, r, c255;
    mul.lo.u32 g, g, a;
    mad.lo.u32 g, dg, inv, g;
    div.u32 g, g, c255;
    mul.lo.u32 b, b, a;
    mad.lo.u32 b, db, inv, b;
    div.u32 b, b, c255;
    st.global.u8 [addr], r;
    st.global.u8 [addr+1], g;
    st.global.u8 [addr+2], b;
    bra DONE;
OUTLINE:
    // BUILD 220: border outline for a pixel the layer does not draw
    ld.param.f32 fow, [outline_w];
    setp.gt.f32 p, fow, 0f00000000;
    @!p bra DONE;
    ld.param.f32 fcrx, [crop_x];
    ld.param.f32 fcry, [crop_y];
    // x: visible columns lo..hi -> fx edges lo - 0.5 .. hi + 0.5
    sub.f32 fol, fcrx, 0f3F000000;
    cvt.rpi.f32.f32 fol, fol;
    max.f32 fol, fol, 0f00000000;
    sub.f32 fol, fol, 0f3F000000;
    cvt.rn.f32.u32 fohi, swv;
    sub.f32 foe, fohi, 0f3F800000;
    sub.f32 fohi, fohi, fcrx;
    sub.f32 fohi, fohi, 0f3F000000;
    cvt.rmi.f32.f32 fohi, fohi;
    min.f32 fohi, fohi, foe;
    add.f32 fohi, fohi, 0f3F000000;
    setp.ge.f32 p, fol, fohi;
    @p bra DONE;
    sub.f32 fod, fol, fx;
    sub.f32 foe, fx, fohi;
    max.f32 fod, fod, foe;
    max.f32 fod, fod, 0f00000000;
    div.rn.f32 fod, fod, invs;
    setp.ge.f32 p, fod, fow;
    @p bra DONE;
    // y: the same with crop_y and sh
    sub.f32 fol, fcry, 0f3F000000;
    cvt.rpi.f32.f32 fol, fol;
    max.f32 fol, fol, 0f00000000;
    sub.f32 fol, fol, 0f3F000000;
    cvt.rn.f32.u32 fohi, shv;
    sub.f32 foe, fohi, 0f3F800000;
    sub.f32 fohi, fohi, fcry;
    sub.f32 fohi, fohi, 0f3F000000;
    cvt.rmi.f32.f32 fohi, fohi;
    min.f32 fohi, fohi, foe;
    add.f32 fohi, fohi, 0f3F000000;
    setp.ge.f32 p, fol, fohi;
    @p bra DONE;
    sub.f32 fodb, fol, fy;
    sub.f32 foe, fy, fohi;
    max.f32 fodb, fodb, foe;
    max.f32 fodb, fodb, 0f00000000;
    div.rn.f32 fodb, fodb, invsy;
    setp.ge.f32 p, fodb, fow;
    @p bra DONE;
    ld.param.u32 orgba, [outline_rgba];
    and.b32 r, orgba, 255;
    shr.b32 g, orgba, 8;
    and.b32 g, g, 255;
    shr.b32 b, orgba, 16;
    and.b32 b, b, 255;
    shr.b32 a, orgba, 24;
    bra COLOR_DONE;
DONE:
    ret;
}
.visible .entry expand_rgba(
    .param .u64 src, .param .u64 dst, .param .u32 count)
{
    .reg .pred p;
    .reg .u32 idx, n, r, g, b;
    .reg .u64 srcv, dstv, saddr, daddr;
    mov.u32 idx, %tid.x;
    mov.u32 n, %ctaid.x;
    mul.lo.u32 n, n, 256;
    add.u32 idx, idx, n;
    ld.param.u32 n, [count];
    setp.ge.u32 p, idx, n;
    @p bra EXPAND_DONE;
    ld.param.u64 srcv, [src];
    mul.lo.u32 n, idx, 3;
    cvt.u64.u32 saddr, n;
    add.u64 saddr, srcv, saddr;
    ld.global.u8 r, [saddr];
    ld.global.u8 g, [saddr+1];
    ld.global.u8 b, [saddr+2];
    ld.param.u64 dstv, [dst];
    mul.lo.u32 n, idx, 4;
    cvt.u64.u32 daddr, n;
    add.u64 daddr, dstv, daddr;
    st.global.u8 [daddr], r;
    st.global.u8 [daddr+1], g;
    st.global.u8 [daddr+2], b;
    mov.u32 r, 255;
    st.global.u8 [daddr+3], r;
EXPAND_DONE:
    ret;
}
"""


def _player_format():
    # OpenGL 2.1 compatibility, no vsync
    from PySide6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setMajorVersion(2)
    fmt.setMinorVersion(1)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setSwapInterval(0)
    return fmt


def _dark_mode():
    return _get_global_setting("dark_mode", "0") == "1"

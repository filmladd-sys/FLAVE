# sequence_player.py
# Sequence Player for Ladd Video Editor: a still preview of the frame under
# the timeline's scrub. Plain QWindow + QOpenGLContext (no QOpenGLWidget),
# NVDEC decode, CUDA scale and composite, one OpenGL present per request.
# No playback loop, no QTimer, no controls. ASCII only.
#
# GPU path follows VidTest_v02.py: cudaGLSetGLDevice on the GL context,
# PyNvVideoCodec SimpleDecoder in device memory (RGB), a PTX kernel that
# scales a layer to the sequence buffer and blends it, then
# cudaMemcpy2DToArray into a registered GL texture and a textured quad.

import ctypes
import os
import threading
import time
import sqlite3

from PySide6.QtCore import Qt, QEvent, QBuffer, QIODevice
from PySide6.QtGui import QImage, QSurfaceFormat, QWindow, QOpenGLContext

from sequence_timeline_sql import (
    _get_global_setting, _set_global_setting, _load_sequence_name,
    _add_open_player_id, _remove_open_player_id,
)

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


def _check(err, name):
    if err != 0:
        raise RuntimeError("%s failed: %s" % (name, err))


def _cuda_bin_dirs():
    # CUDA_PATH / CUDA_PATH_V* bin folders, then the toolkit v*\bin folders,
    # each also with an x64 subfolder
    import glob
    cuda_dirs = []
    for key, val in sorted(os.environ.items()):
        if key.upper() == "CUDA_PATH" or key.upper().startswith("CUDA_PATH_V"):
            if val:
                cuda_dirs.append(os.path.join(val, "bin"))
    toolkit = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
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
    for fn in ("nvjpegCreateSimple", "nvjpegDestroy", "nvjpegEncoderStateCreate",
               "nvjpegEncoderStateDestroy", "nvjpegEncoderParamsCreate",
               "nvjpegEncoderParamsDestroy", "nvjpegEncoderParamsSetQuality",
               "nvjpegEncoderParamsSetSamplingFactors", "nvjpegEncodeImage",
               "nvjpegEncodeRetrieveBitstream"):
        getattr(lib, fn).restype = c.c_int
    _NVJPEG[0] = lib
    return lib


class _NvJpegEncoder(object):
    # One nvJPEG encoder per player (its CUDA context and stream). Encodes
    # an interleaved RGB device buffer; only the JPEG bytes come back.
    def __init__(self, lib, stream):
        self.lib = lib
        self.stream = stream
        self.handle = ctypes.c_void_p()
        self.state = ctypes.c_void_p()
        self.params = ctypes.c_void_p()
        self.quality = None
        self.states = []                 # encode_many: one state per buffer
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
        # nvJPEG has no batched encode: queue every encode on the stream
        # (one encoder state per buffer), sync once, then read the N
        # bitstreams. Pixels stay on the GPU.
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
    toolkit = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
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
    # Load cudart, nvcuda and opengl32 once, with the same signatures as
    # VidTest_v02.py plus the few calls a still preview needs
    global _LIBS
    if _LIBS is not None:
        return _LIBS
    c = ctypes
    cudart = _load_cudart()
    cudart.cudaGLSetGLDevice.argtypes = [c.c_int]
    cudart.cudaGLSetGLDevice.restype = c.c_int
    cudart.cudaMalloc.argtypes = [c.POINTER(c.c_void_p), c.c_size_t]
    cudart.cudaMalloc.restype = c.c_int
    cudart.cudaFree.argtypes = [c.c_void_p]
    cudart.cudaFree.restype = c.c_int
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
# Source pixel = dest pixel + (ox, oy); outside the source is left as is,
# so a larger source is center-cropped and a smaller one is centered.
# 3 bytes per pixel (NVDEC RGB) is opaque; 4 uses the source alpha.
PTX = b"""
.version 7.0
.target sm_75
.address_size 64
.visible .entry blend_layer(
    .param .u64 src, .param .u64 dst,
    .param .u32 sw, .param .u32 sh, .param .u32 sstride, .param .u32 bpp,
    .param .u32 dw, .param .u32 dh, .param .s32 ox, .param .s32 oy)
{
    .reg .pred p;
    .reg .u32 idx, x, y, dwv, dhv, swv, shv, ssv, bppv, pix;
    .reg .u32 sx, sy, off32, r, g, b, a, dr, dg, db, inv, c255;
    .reg .s32 oxv, oyv;
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
    add.s32 sx, x, oxv;
    add.s32 sy, y, oyv;
    setp.lt.s32 p, sx, 0;
    @p bra DONE;
    setp.lt.s32 p, sy, 0;
    @p bra DONE;
    setp.ge.s32 p, sx, swv;
    @p bra DONE;
    setp.ge.s32 p, sy, shv;
    @p bra DONE;
    mul.lo.u32 off32, sy, ssv;
    mad.lo.u32 off32, sx, bppv, off32;
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
DONE:
    ret;
}
"""


def _player_format():
    fmt = QSurfaceFormat()
    fmt.setMajorVersion(2)
    fmt.setMinorVersion(1)
    fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
    fmt.setSwapInterval(0)
    return fmt


def _dark_mode():
    return _get_global_setting("dark_mode", "0") == "1"


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
        self.dst = ctypes.c_void_p()     # sequence-size RGB composite buffer
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
        gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB8, self.seq_w, self.seq_h, 0,
                        GL_RGB, GL_UNSIGNED_BYTE, None)
        # RGB cannot be registered as a CUDA image: present through a PBO
        self._glb = self._gl_buffer_funcs(gl)
        gen, bind, data = self._glb[0], self._glb[1], self._glb[2]
        gen(1, ctypes.byref(self.pbo))
        bind(GL_PIXEL_UNPACK_BUFFER, self.pbo.value)
        data(GL_PIXEL_UNPACK_BUFFER, self.row * self.seq_h, None, GL_STREAM_DRAW)
        bind(GL_PIXEL_UNPACK_BUFFER, 0)
        _check(cudart.cudaGraphicsGLRegisterBuffer(ctypes.byref(self.res),
                                                   self.pbo.value, 2), "register pbo")
        _check(cudart.cudaMalloc(ctypes.byref(self.dst), self.row * self.seq_h), "dst malloc")
        _check(cudart.cudaMalloc(ctypes.byref(self.gen_dst), self.row * self.seq_h),
               "gen dst malloc")
        module = ctypes.c_void_p()
        _check(nvcuda.cuModuleLoadData(ctypes.byref(module), PTX), "module")
        _check(nvcuda.cuModuleGetFunction(ctypes.byref(self.kernel), module,
                                          b"blend_layer"), "kernel")
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
            if self.res.value:
                cudart.cudaGraphicsUnregisterResource(self.res)
            if self.pbo.value and self._glb is not None:
                self._glb[3](1, ctypes.byref(self.pbo))
            if self.dst.value:
                cudart.cudaFree(self.dst)
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
        self._tm["dec"] = "reused" if dec is not None else "created"
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
            self._tm["dec"] = "image cached"
            return self.images[path]
        self._tm["dec"] = "image uploaded"
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

    # ---- one frame: seek, decode, scale and composite, present once

    def request_frame(self, force=True):
        # force False (scrub): skip when the frame is the one already shown
        if self.closed or not self.isExposed():
            return
        frame = int(self.timeline.current_frame)
        if not force and frame == self.shown_frame:
            return
        # Timing debug: one PLAYER line per request
        t_req = time.perf_counter()
        t_hook = getattr(self, "_t_hook", None)
        self._t_hook = None
        self._tm = {"dec": "none", "size": "none", "open": 0.0, "len": 0.0,
                    "seek": 0.0, "decode": 0.0, "copy": 0.0, "blend": 0.0,
                    "clear": 0.0, "sync": 0.0, "map": 0.0, "tex": 0.0,
                    "unmap": 0.0, "draw": 0.0, "swap": 0.0}
        dark = _dark_mode()
        if not self.ready:
            self._present(dark, False)
            return
        try:
            self._composite(frame, dark)
            self._present(dark, True)
            self.shown_frame = frame
        except Exception as e:
            print(f"Sequence Player frame failed: {e}")
            self._present(dark, False)
        t_end = time.perf_counter()
        tm = self._tm
        ms = lambda v: "%.1f" % (v * 1000.0)
        hook = ms(t_req - t_hook) if t_hook is not None else "0"
        present = tm["draw"] + tm["swap"]
        # print("PLAYER: frame=%d hook=%s dec=%s size=%s open=%s len=%s seek=%s "
        #       "decode=%s copy=%s clear=%s blend=%s sync=%s map=%s tex=%s unmap=%s "
        #       "draw=%s swap=%s present=%s total=%s" % (
        #           frame, hook, tm["dec"], tm["size"], ms(tm["open"]), ms(tm["len"]),
        #           ms(tm["seek"]), ms(tm["decode"]), ms(tm["copy"]), ms(tm["clear"]),
        #           ms(tm["blend"]), ms(tm["sync"]), ms(tm["map"]), ms(tm["tex"]),
        #           ms(tm["unmap"]), ms(tm["draw"]), ms(tm["swap"]), ms(present),
        #           ms(t_end - (t_hook if t_hook is not None else t_req))))

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
        tm = self._tm
        t0 = time.perf_counter()
        _check(cudart.cudaMemsetAsync(target, 0 if dark else 255,
                                      self.row * self.seq_h, self.stream), "clear")
        tm["clear"] += time.perf_counter() - t0
        for kind, path, src_frame in self._layers(frame, snapshot):
            if kind == "image":
                t0 = time.perf_counter()
                item = self._image(path)
                tm["copy"] += time.perf_counter() - t0
                if item is not None:
                    tm["size"] = "%dx%d" % (item[1], item[2])
                    t0 = time.perf_counter()
                    self._blend(item[0].value, item[1], item[2], item[3], 4)
                    tm["blend"] += time.perf_counter() - t0
                continue
            if not os.path.isfile(path):
                continue
            if prefetched is not None and (path, src_frame) in prefetched:
                # Decoded by a multi-frame get_batch_frames: no seek, no decode
                f = prefetched[(path, src_frame)]
                shape = [int(v) for v in f.shape]
                sh, sw = shape[0], shape[1]
                tm["size"] = "%dx%d" % (sw, sh)
                stride = [int(v) for v in f.strides][0] if f.strides else sw * 3
                t0 = time.perf_counter()
                self._blend(int(f.GetPtrToPlane(0)), sw, sh, stride, 3, sync)
                tm["blend"] += time.perf_counter() - t0
                continue
            t0 = time.perf_counter()
            dec = self._decoder(path)
            tm["open"] += time.perf_counter() - t0
            # Seek only when the asked frame is not the next one this decoder
            # yields; a run in order continues decoding (HEVC GOP 15)
            t0 = time.perf_counter()
            if self.dec_next.get(path) != src_frame:
                dec.seek_to_index(src_frame)
                tm["seeked"] = 1
            tm["seek"] += time.perf_counter() - t0
            t0 = time.perf_counter()
            try:
                batch = dec.get_batch_frames(1)
            except Exception:
                self.dec_next.pop(path, None)
                raise
            tm["decode"] += time.perf_counter() - t0
            if not batch:
                self.dec_next.pop(path, None)
                continue
            self.dec_next[path] = src_frame + 1
            f = batch[0]
            shape = [int(v) for v in f.shape]
            sh, sw = shape[0], shape[1]
            tm["size"] = "%dx%d" % (sw, sh)
            stride = [int(v) for v in f.strides][0] if f.strides else sw * 3
            # copy stays 0 for video: the kernel reads the decoder frame directly
            t0 = time.perf_counter()
            self._blend(int(f.GetPtrToPlane(0)), sw, sh, stride, 3)
            tm["blend"] += time.perf_counter() - t0
        t0 = time.perf_counter()
        self._sync()
        tm["sync"] += time.perf_counter() - t0
        self._target = None
        if target is self.dst:
            self._dst_frame = frame

    def _composite(self, frame, dark):
        with _GuiGen():
            self._composite_locked(frame, dark)

    def _composite_locked(self, frame, dark):
        self._tm = _new_tm()
        self._build_dst(frame, dark)
        cudart = _libs()[0]
        tm = self._tm
        gl = _libs()[2]
        res_arr = (ctypes.c_void_p * 1)(self.res)
        t0 = time.perf_counter()
        _check(cudart.cudaGraphicsMapResources(1, res_arr, None), "map")
        tm["map"] += time.perf_counter() - t0
        try:
            t0 = time.perf_counter()
            ptr = ctypes.c_void_p()
            size = ctypes.c_size_t(0)
            _check(cudart.cudaGraphicsResourceGetMappedPointer(
                ctypes.byref(ptr), ctypes.byref(size), self.res), "pbo pointer")
            _check(cudart.cudaMemcpy(ptr, self.dst, self.row * self.seq_h, 3), "to pbo")
            tm["tex"] += time.perf_counter() - t0
        finally:
            t0 = time.perf_counter()
            _check(cudart.cudaGraphicsUnmapResources(1, res_arr, None), "unmap")
            tm["unmap"] += time.perf_counter() - t0
        # PBO (RGB, tightly packed rows) into the texture, on the GPU
        t0 = time.perf_counter()
        bind = self._glb[1]
        bind(GL_PIXEL_UNPACK_BUFFER, self.pbo.value)
        gl.glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        gl.glBindTexture(GL_TEXTURE_2D, self.tex)
        gl.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.seq_w, self.seq_h,
                           GL_RGB, GL_UNSIGNED_BYTE, None)
        bind(GL_PIXEL_UNPACK_BUFFER, 0)
        tm["tex"] += time.perf_counter() - t0

    def _present(self, dark, has_frame):
        if self.ctx is None:
            return
        _cudart, _nvcuda, gl = _libs()
        t0 = time.perf_counter()
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
        tm = getattr(self, "_tm", None)
        t1 = time.perf_counter()
        self.ctx.swapBuffers(self)
        if tm is not None:
            tm["draw"] += t1 - t0
            tm["swap"] += time.perf_counter() - t1


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

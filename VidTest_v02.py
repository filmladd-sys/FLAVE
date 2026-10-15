import sys
import ctypes
import threading
import time
from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QImage, QSurfaceFormat, QWindow, QOpenGLContext
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QSlider
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput

import PyNvVideoCodec as nvc

VIDEO_PATH = r"P:\PythonVidTest\VidTest.mov"
OVERLAY_PATH = r"P:\PythonVidTest\Overlay.png"
FRAME_W = 1920
FRAME_H = 1080
ROW = FRAME_W * 4
BATCH = 30
PARK_SLOTS = 40
DISP_SLOTS = 40
GOP = 15

def load_cudart():
    for name in ("cudart64_12.dll", "cudart64_13.dll", "cudart64_11.dll"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            pass
    raise RuntimeError("cudart64 dll not found")

cudart = load_cudart()
cudart.cudaGLSetGLDevice.argtypes = [ctypes.c_int]
cudart.cudaGLSetGLDevice.restype = ctypes.c_int
cudart.cudaMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
cudart.cudaMalloc.restype = ctypes.c_int
cudart.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
cudart.cudaMemcpy.restype = ctypes.c_int
cudart.cudaMemcpy2DToArray.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int]
cudart.cudaMemcpy2DToArray.restype = ctypes.c_int
cudart.cudaGraphicsGLRegisterImage.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
cudart.cudaGraphicsGLRegisterImage.restype = ctypes.c_int
cudart.cudaGraphicsMapResources.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
cudart.cudaGraphicsMapResources.restype = ctypes.c_int
cudart.cudaGraphicsUnmapResources.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
cudart.cudaGraphicsUnmapResources.restype = ctypes.c_int
cudart.cudaGraphicsSubResourceGetMappedArray.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
cudart.cudaGraphicsSubResourceGetMappedArray.restype = ctypes.c_int
cudart.cudaEventCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
cudart.cudaEventCreate.restype = ctypes.c_int
cudart.cudaEventRecord.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
cudart.cudaEventRecord.restype = ctypes.c_int
cudart.cudaEventSynchronize.argtypes = [ctypes.c_void_p]
cudart.cudaEventSynchronize.restype = ctypes.c_int
cudart.cudaStreamCreate.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
cudart.cudaStreamCreate.restype = ctypes.c_int

gl = ctypes.WinDLL("opengl32.dll")
gl.glGenTextures.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
gl.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
gl.glTexParameteri.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
gl.glTexImage2D.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
gl.glEnable.argtypes = [ctypes.c_uint]
gl.glBegin.argtypes = [ctypes.c_uint]
gl.glEnd.argtypes = []
gl.glTexCoord2f.argtypes = [ctypes.c_float, ctypes.c_float]
gl.glVertex2f.argtypes = [ctypes.c_float, ctypes.c_float]
gl.glClear.argtypes = [ctypes.c_uint]
gl.glClearColor.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]
gl.glViewport.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]

GL_TEXTURE_2D = 0x0DE1
GL_RGBA = 0x1908
GL_RGBA8 = 0x8058
GL_UNSIGNED_BYTE = 0x1401
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_COLOR_BUFFER_BIT = 0x4000
GL_QUADS = 0x0007

def check(err, name):
    if err != 0:
        raise RuntimeError("%s failed: %s" % (name, err))

nvcuda = ctypes.WinDLL("nvcuda.dll")
nvcuda.cuInit.argtypes = [ctypes.c_uint]
nvcuda.cuInit.restype = ctypes.c_int
nvcuda.cuCtxGetCurrent.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
nvcuda.cuCtxGetCurrent.restype = ctypes.c_int
nvcuda.cuCtxSetCurrent.argtypes = [ctypes.c_void_p]
nvcuda.cuCtxSetCurrent.restype = ctypes.c_int
nvcuda.cuModuleLoadData.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p]
nvcuda.cuModuleLoadData.restype = ctypes.c_int
nvcuda.cuModuleGetFunction.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_char_p]
nvcuda.cuModuleGetFunction.restype = ctypes.c_int
nvcuda.cuLaunchKernel.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
    ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
    ctypes.c_uint, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
]
nvcuda.cuLaunchKernel.restype = ctypes.c_int
check(nvcuda.cuInit(0), "cuInit")

PTX = b"""
.version 7.0
.target sm_75
.address_size 64
.visible .entry scale_overlay(
    .param .u64 src, .param .u64 dst, .param .u64 ovl,
    .param .u32 sw, .param .u32 sh, .param .u32 sstride,
    .param .u32 dw, .param .u32 dh, .param .u32 ow, .param .u32 oh)
{
    .reg .pred pskip;
    .reg .pred poutside;
    .reg .u32 idx, x, y, dwv, dhv, swv, shv, ssv, owv, ohv, pix;
    .reg .u32 sx, sy, off32, r, g, b, oa, orr, og, ob, inv, eight, twofive, ox, oy;
    .reg .u64 srcv, dstv, ovv, addr;
    mov.u32 idx, %tid.x;
    mov.u32 x, %ctaid.x;
    mul.lo.u32 x, x, 256;
    add.u32 idx, idx, x;
    ld.param.u32 dwv, [dw];
    ld.param.u32 dhv, [dh];
    mul.lo.u32 pix, dwv, dhv;
    setp.ge.u32 pskip, idx, pix;
    @pskip bra DONE;
    div.u32 y, idx, dwv;
    mul.lo.u32 x, y, dwv;
    sub.u32 x, idx, x;
    ld.param.u32 swv, [sw];
    ld.param.u32 shv, [sh];
    ld.param.u32 ssv, [sstride];
    mul.lo.u32 sx, x, swv;
    div.u32 sx, sx, dwv;
    mul.lo.u32 sy, y, shv;
    div.u32 sy, sy, dhv;
    mul.lo.u32 off32, sy, ssv;
    mul.lo.u32 sy, sx, 3;
    add.u32 off32, off32, sy;
    cvt.u64.u32 addr, off32;
    ld.param.u64 srcv, [src];
    add.u64 addr, srcv, addr;
    ld.global.u8 r, [addr];
    ld.global.u8 g, [addr+1];
    ld.global.u8 b, [addr+2];
    ld.param.u32 owv, [ow];
    ld.param.u32 ohv, [oh];
    setp.eq.u32 poutside, owv, 0;
    @poutside bra STORE;
    mul.lo.u32 ox, x, owv;
    div.u32 ox, ox, dwv;
    mul.lo.u32 oy, y, ohv;
    div.u32 oy, oy, dhv;
    mad.lo.u32 off32, oy, owv, ox;
    mul.lo.u32 off32, off32, 4;
    cvt.u64.u32 addr, off32;
    ld.param.u64 ovv, [ovl];
    add.u64 addr, ovv, addr;
    ld.global.u8 orr, [addr];
    ld.global.u8 og, [addr+1];
    ld.global.u8 ob, [addr+2];
    ld.global.u8 oa, [addr+3];
    mov.u32 eight, 255;
    sub.u32 inv, eight, oa;
    mul.lo.u32 r, r, inv;
    mul.lo.u32 orr, orr, oa;
    add.u32 r, r, orr;
    mov.u32 eight, 8;
    shr.u32 r, r, eight;
    mul.lo.u32 g, g, inv;
    mul.lo.u32 og, og, oa;
    add.u32 g, g, og;
    shr.u32 g, g, eight;
    mul.lo.u32 b, b, inv;
    mul.lo.u32 ob, ob, oa;
    add.u32 b, b, ob;
    shr.u32 b, b, eight;
STORE:
    mul.lo.u32 off32, idx, 4;
    cvt.u64.u32 addr, off32;
    ld.param.u64 dstv, [dst];
    add.u64 addr, dstv, addr;
    st.global.u8 [addr], r;
    st.global.u8 [addr+1], g;
    st.global.u8 [addr+2], b;
    mov.u32 twofive, 255;
    st.global.u8 [addr+3], twofive;
DONE:
    ret;
}
"""

fmt = QSurfaceFormat()
fmt.setMajorVersion(2)
fmt.setMinorVersion(1)
fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
fmt.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(fmt)
app = QApplication(sys.argv)

class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.park_ready = threading.Condition(self.lock)
        self.disp_ready = threading.Condition(self.lock)
        self.park_slots = []
        self.park_free = []
        self.park_q = []
        self.disp_slots = []
        self.disp_free = []
        self.disp_q = []
        self.seek = 0
        self.need_seek = True
        self.stop = False
        self.fps = 29.97
        self.frame_count = 0
        self.sw = 0
        self.sh = 0
        self.stride = 0
        self.kernel = ctypes.c_void_p()
        self.stream = ctypes.c_void_p()
        self.ov_ptr = ctypes.c_void_p()
        self.ov_w = 0
        self.ov_h = 0
        self.cuda_ctx = ctypes.c_void_p()

S = Shared()

def park_worker():
    try:
        check(nvcuda.cuCtxSetCurrent(S.cuda_ctx), "park ctx")
        decoder = nvc.SimpleDecoder(
            VIDEO_PATH,
            gpu_id=0,
            use_device_memory=True,
            need_scanned_stream_metadata=True,
            output_color_type=nvc.OutputColorType.RGB,
        )
        meta = decoder.get_stream_metadata()
        fps = float(getattr(meta, "avg_frame_rate", 29.97) or 29.97)
        if fps < 1.0:
            fps = 29.97
        with S.lock:
            S.fps = fps
            S.frame_count = len(decoder)
            S.park_ready.notify_all()
        index = 0
        while True:
            with S.lock:
                if S.stop:
                    return
                if S.need_seek:
                    index = S.seek
                    S.need_seek = False
                    S.park_q = []
                    S.park_free = list(range(len(S.park_slots)))
                    do_seek = True
                else:
                    do_seek = False
                while S.park_slots and not S.park_free and not S.stop and not S.need_seek:
                    S.park_ready.wait(0.02)
                if S.stop or S.need_seek:
                    continue
                slot = S.park_free.pop(0) if S.park_slots else 0
            if do_seek:
                decoder.seek_to_index(index)
            batch = decoder.get_batch_frames(1)
            if not batch:
                continue
            frame = batch[0]
            shape = [int(v) for v in frame.shape]
            sh, sw = shape[0], shape[1]
            stride = [int(v) for v in frame.strides][0] if frame.strides else sw * 3
            nbytes = sh * stride
            with S.lock:
                if not S.park_slots:
                    S.sw, S.sh, S.stride = sw, sh, stride
                    for _ in range(PARK_SLOTS):
                        p = ctypes.c_void_p()
                        check(cudart.cudaMalloc(ctypes.byref(p), nbytes), "park malloc")
                        S.park_slots.append(p)
                    S.park_free = list(range(1, PARK_SLOTS))
                    slot = 0
                    for _ in range(DISP_SLOTS):
                        d = ctypes.c_void_p()
                        check(cudart.cudaMalloc(ctypes.byref(d), ROW * FRAME_H), "disp malloc")
                        S.disp_slots.append(d)
                        S.disp_free.append(len(S.disp_slots) - 1)
            src = ctypes.c_void_p(int(frame.GetPtrToPlane(0)))
            check(cudart.cudaMemcpy(S.park_slots[slot], src, nbytes, 3), "park copy")
            with S.lock:
                if S.need_seek:
                    S.park_free.append(slot)
                    continue
                S.park_q.append((index, slot))
                S.park_ready.notify_all()
            index += 1
            if index >= len(decoder):
                with S.lock:
                    while not S.need_seek and not S.stop:
                        S.park_ready.wait(0.05)
    except Exception as exc:
        print("park failed:", exc)

def process_worker():
    try:
        check(nvcuda.cuCtxSetCurrent(S.cuda_ctx), "proc ctx")
        while True:
            with S.lock:
                if S.stop:
                    return
                while not S.park_q and not S.stop and not S.need_seek:
                    S.park_ready.wait(0.02)
                if S.stop:
                    return
                if S.need_seek or not S.park_q:
                    continue
                index, pslot = S.park_q.pop(0)
                while not S.disp_free and not S.stop:
                    S.disp_ready.wait(0.02)
                if S.stop:
                    return
                dslot = S.disp_free.pop(0)
            ev = ctypes.c_void_p()
            check(cudart.cudaEventCreate(ctypes.byref(ev)), "event")
            src = ctypes.c_uint64(int(S.park_slots[pslot].value))
            dst = ctypes.c_uint64(int(S.disp_slots[dslot].value))
            ov = ctypes.c_uint64(int(S.ov_ptr.value or 0))
            sw_p = ctypes.c_uint32(S.sw)
            sh_p = ctypes.c_uint32(S.sh)
            ss_p = ctypes.c_uint32(S.stride)
            dw_p = ctypes.c_uint32(FRAME_W)
            dh_p = ctypes.c_uint32(FRAME_H)
            ow_p = ctypes.c_uint32(S.ov_w)
            oh_p = ctypes.c_uint32(S.ov_h)
            arglist = (ctypes.c_void_p * 10)(
                ctypes.cast(ctypes.pointer(src), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(dst), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(ov), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(sw_p), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(sh_p), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(ss_p), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(dw_p), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(dh_p), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(ow_p), ctypes.c_void_p),
                ctypes.cast(ctypes.pointer(oh_p), ctypes.c_void_p),
            )
            grid = (FRAME_W * FRAME_H + 255) // 256
            check(nvcuda.cuLaunchKernel(
                S.kernel, grid, 1, 1, 256, 1, 1, 0, S.stream, arglist, None
            ), "launch")
            check(cudart.cudaEventRecord(ev, S.stream), "record")
            check(cudart.cudaEventSynchronize(ev), "sync")
            with S.lock:
                if S.need_seek:
                    S.disp_free.append(dslot)
                    S.park_free.append(pslot)
                    continue
                S.disp_q.append((index, dslot, pslot))
                S.disp_ready.notify_all()
    except Exception as exc:
        print("process failed:", exc)

class SequencePlayer(QWindow):
    def __init__(self):
        super().__init__()
        self.setSurfaceType(QWindow.OpenGLSurface)
        self.setFormat(fmt)
        self.ctx = None
        self.index = -1
        self.playing = False
        self.scrub = None
        self.audio = None
        self.texs = []
        self.res = []
        self.batch = []
        self.batch_i = 0
        self.has_frame = False
        self.ready = False
        self.setTitle("Sequence Player v02")
        self.resize(FRAME_W, FRAME_H)

    def exposeEvent(self, event):
        if self.isExposed() and not self.ready:
            self.setup()

    def setup(self):
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(fmt)
        self.ctx.create()
        self.ctx.makeCurrent(self)
        check(cudart.cudaGLSetGLDevice(0), "cudaGLSetGLDevice")
        c = ctypes.c_void_p()
        check(nvcuda.cuCtxGetCurrent(ctypes.byref(c)), "cuCtxGetCurrent")
        S.cuda_ctx = c
        check(cudart.cudaStreamCreate(ctypes.byref(S.stream)), "stream")
        for _ in range(BATCH):
            tex = ctypes.c_uint(0)
            gl.glGenTextures(1, ctypes.byref(tex))
            gl.glBindTexture(GL_TEXTURE_2D, tex)
            gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
            gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
            gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, FRAME_W, FRAME_H, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
            r = ctypes.c_void_p()
            check(cudart.cudaGraphicsGLRegisterImage(ctypes.byref(r), tex, GL_TEXTURE_2D, 2), "register tex")
            self.texs.append(tex)
            self.res.append(r)
        print("textures registered", BATCH)
        module = ctypes.c_void_p()
        check(nvcuda.cuModuleLoadData(ctypes.byref(module), PTX), "module")
        check(nvcuda.cuModuleGetFunction(ctypes.byref(S.kernel), module, b"scale_overlay"), "kernel")
        overlay = QImage(OVERLAY_PATH).convertToFormat(QImage.Format_RGBA8888)
        if overlay.isNull():
            print("Overlay missing")
        else:
            S.ov_w = overlay.width()
            S.ov_h = overlay.height()
            raw = bytes(overlay.constBits())[: overlay.sizeInBytes()]
            check(cudart.cudaMalloc(ctypes.byref(S.ov_ptr), len(raw)), "ov malloc")
            check(cudart.cudaMemcpy(S.ov_ptr, raw, len(raw), 1), "ov copy")
            print("Overlay used as-is:", S.ov_w, "x", S.ov_h)
        self.pth = threading.Thread(target=park_worker, daemon=True)
        self.cth = threading.Thread(target=process_worker, daemon=True)
        self.pth.start()
        self.cth.start()
        with S.lock:
            while S.frame_count == 0:
                S.park_ready.wait(0.1)
        self.fps = S.fps
        self.frame_count = S.frame_count
        print("v02", FRAME_W, "x", FRAME_H, "batch", BATCH, "frames", self.frame_count, "fps", self.fps)
        self.fill_batch(True)
        self.index = self.batch[0][0]
        self.has_frame = True
        self.ready = True
        self.draw()

    def wait_batch(self):
        while True:
            with S.lock:
                depth = len(S.disp_q)
            if depth >= BATCH:
                return
            app.processEvents()
            threading.Event().wait(0.002)

    def fill_batch(self, block):
        if block:
            self.wait_batch()
        with S.lock:
            if len(S.disp_q) < BATCH:
                return False
            items = [S.disp_q.pop(0) for _ in range(BATCH)]
        self.ctx.makeCurrent(self)
        check(nvcuda.cuCtxSetCurrent(S.cuda_ctx), "batch ctx")
        res_arr = (ctypes.c_void_p * BATCH)(*[r for r in self.res])
        t0 = time.perf_counter()
        check(cudart.cudaGraphicsMapResources(BATCH, res_arr, None), "map batch")
        for i, item in enumerate(items):
            arr = ctypes.c_void_p()
            check(cudart.cudaGraphicsSubResourceGetMappedArray(ctypes.byref(arr), self.res[i], 0, 0), "array")
            check(cudart.cudaMemcpy2DToArray(arr, 0, 0, S.disp_slots[item[1]], ROW, ROW, FRAME_H, 3), "to tex")
        check(cudart.cudaGraphicsUnmapResources(BATCH, res_arr, None), "unmap batch")
        print("batch write ms", round((time.perf_counter() - t0) * 1000.0, 1))
        with S.lock:
            for item in items:
                S.disp_free.append(item[1])
                S.park_free.append(item[2])
            S.park_ready.notify_all()
            S.disp_ready.notify_all()
        self.batch = [(item[0], i) for i, item in enumerate(items)]
        self.batch_i = 0
        return True

    def request_seek(self, index):
        with S.lock:
            S.seek = index
            S.need_seek = True
            S.park_q = []
            S.disp_q = []
            S.park_ready.notify_all()
            S.disp_ready.notify_all()
        self.batch = []
        self.batch_i = 0

    def draw(self):
        if not self.ready or not self.isExposed():
            return
        self.ctx.makeCurrent(self)
        gl.glViewport(0, 0, self.width(), self.height())
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(GL_COLOR_BUFFER_BIT)
        if self.has_frame and self.batch:
            which = self.batch[self.batch_i - 1][1] if self.batch_i else self.batch[0][1]
            gl.glEnable(GL_TEXTURE_2D)
            gl.glBindTexture(GL_TEXTURE_2D, self.texs[which])
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

    def audio_frame(self):
        if not self.audio:
            return self.index
        ms = self.audio.position()
        return int(ms * self.fps / 1000.0)

    def present_next(self):
        if self.batch_i >= len(self.batch):
            if not self.fill_batch(False):
                return False
        self.index = self.batch[self.batch_i][0]
        self.batch_i += 1
        self.has_frame = True
        self.draw()
        return True

    def catch_up(self, target):
        while self.batch and self.batch_i < len(self.batch) and self.batch[self.batch_i][0] < target:
            self.batch_i += 1
        if self.batch_i >= len(self.batch):
            self.fill_batch(False)
            while self.batch and self.batch_i < len(self.batch) and self.batch[self.batch_i][0] < target:
                self.batch_i += 1

    def run_loop(self):
        shown = 0
        t0 = time.perf_counter()
        while self.playing and self.index + 1 < self.frame_count:
            want = self.audio_frame()
            due = int((self.index + 1) * 1000.0 / self.fps)
            if self.audio and self.audio.position() + 1 < due:
                app.processEvents()
                threading.Event().wait(0.001)
                continue
            if want > self.index + 2:
                self.catch_up(want)
            if self.present_next():
                shown += 1
            now = time.perf_counter()
            if now - t0 >= 1.0:
                with S.lock:
                    print("paced fps", shown, "park", len(S.park_q), "disp", len(S.disp_q), "audio", self.audio_frame())
                shown = 0
                t0 = now
            if self.scrub:
                self.scrub.setValue(self.index)
            app.processEvents()
        self.playing = False
        button.setText("Play")
        if self.audio:
            self.audio.pause()

player = SequencePlayer()
audio_out = QAudioOutput()
audio = QMediaPlayer()
audio.setAudioOutput(audio_out)
audio.setSource(QUrl.fromLocalFile(VIDEO_PATH))
player.audio = audio
player.show()

controls = QWidget()
controls.setWindowTitle("Controls")
controls.setWindowFlags(controls.windowFlags() | Qt.WindowStaysOnTopHint)
controls.resize(640, 140)
button = QPushButton("Play")
skip = QPushButton("skip")
scrub = QSlider(Qt.Horizontal)

def arm():
    scrub.setRange(0, max(0, player.frame_count - 1))
    player.scrub = scrub

QTimer.singleShot(1500, arm)

def on_button():
    if player.playing:
        player.playing = False
        button.setText("Play")
    else:
        audio.setPosition(int(max(0, player.index) * 1000.0 / player.fps))
        player.playing = True
        button.setText("Stop")
        audio.play()
        QTimer.singleShot(0, player.run_loop)

def on_skip():
    player.index = min(player.frame_count - 1, player.index + int(player.fps * 10))
    player.request_seek(player.index)
    player.fill_batch(True)
    player.present_next()
    scrub.setValue(player.index)
    audio.setPosition(int(player.index * 1000.0 / player.fps))

def on_pressed():
    player.playing = False
    audio.pause()

def on_released():
    player.index = scrub.value()
    player.request_seek(player.index)
    player.fill_batch(True)
    player.present_next()
    audio.setPosition(int(player.index * 1000.0 / player.fps))
    if button.text() == "Stop":
        player.playing = True
        audio.play()
        QTimer.singleShot(0, player.run_loop)

button.clicked.connect(on_button)
skip.clicked.connect(on_skip)
scrub.sliderPressed.connect(on_pressed)
scrub.sliderReleased.connect(on_released)
layout = QVBoxLayout()
layout.addWidget(button)
layout.addWidget(skip)
layout.addWidget(scrub)
controls.setLayout(layout)
controls.show()
sys.exit(app.exec())
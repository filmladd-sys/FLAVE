# cpu_decoder.py
# BUILD 342: when NVDEC cannot open one footage file, decode that file on
# the CPU. The decision is per path (this file only). Other footage keeps
# opening NVDEC. There is no process-wide CPU mode.
# Software decode is ffmpeg (the same CPU decode the proxy generator
# already uses), uploading RGB into device memory so the blend kernel
# reads it like a SimpleDecoder frame. ASCII only.

import ctypes
import os
import subprocess
import sys
import threading


# Paths whose NVDEC open has already failed this session, keyed by
# (normcase abspath, size, mtime). Not a global switch: a path absent
# from this set still opens NVDEC. A rewritten file (new size or mtime)
# is tried on NVDEC again.
_LOCK = threading.Lock()
_NVDEC_FAILED = set()
# Passthrough argv for this ffmpeg. None until the first CPU open.
_SYNC_ARGS = None


def _footage_key(path):
    try:
        base = os.path.normcase(os.path.abspath(path))
    except Exception:
        base = os.path.normcase(str(path or ""))
    try:
        st = os.stat(path)
        return (base, int(st.st_size), int(st.st_mtime))
    except OSError:
        return (base, None, None)


def _one_line(text, limit=180):
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        s = s[:limit - 3] + "..."
    return s


def _nvdec_reason(exc):
    text = _one_line(exc, 400)
    low = text.lower()
    if "error 300" in low or "returned error 300" in low:
        return "error 300"
    if len(text) > 160:
        text = text[:157] + "..."
    return text or type(exc).__name__


def footage_decode_error(path, detail):
    # Error text that names the footage. Render shows this string.
    detail = _one_line(detail, 240)
    if detail:
        return RuntimeError("Could not decode footage: %s (%s)" % (path, detail))
    return RuntimeError("Could not decode footage: %s" % path)


def _remember_nvdec_failure(key):
    # True the first time this file is recorded
    with _LOCK:
        first = key not in _NVDEC_FAILED
        _NVDEC_FAILED.add(key)
    return first


def _nvdec_known_failed(key):
    with _LOCK:
        return key in _NVDEC_FAILED


def open_with_cpu_fallback(path, open_nvdec, open_cpu):
    # Try NVDEC for this path. On any open failure, open a CPU decoder for
    # this path only and remember the path so a later instance of the same
    # file does not open NVDEC again. Any other path still calls open_nvdec.
    # open_nvdec / open_cpu are callables. CPU open failure raises an error
    # that names path.
    key = _footage_key(path)
    if not _nvdec_known_failed(key):
        try:
            dec = open_nvdec()
        except Exception as exc:
            if _remember_nvdec_failure(key):
                print("WORKER decode: NVDEC open failed (%s); "
                      "CPU fallback for this file only: %s"
                      % (_nvdec_reason(exc), path))
        else:
            if dec is not None:
                return dec
            if _remember_nvdec_failure(key):
                print("WORKER decode: NVDEC open failed (no decoder); "
                      "CPU fallback for this file only: %s" % path)
    try:
        dec = open_cpu()
    except Exception as exc:
        detail = _one_line(exc)
        prefix = "Could not decode footage: %s" % path
        if detail.startswith(prefix):
            detail = detail[len(prefix):].strip(" ()")
        raise footage_decode_error(
            path, "NVDEC open failed; CPU: %s" % (detail or "failed"))
    if dec is None:
        raise footage_decode_error(
            path, "NVDEC open failed; CPU: no decoder")
    return dec


def _no_window_flags():
    return 0x08000000 if sys.platform.startswith("win") else 0


def _popen(cmd, **kw):
    if sys.platform.startswith("win"):
        kw["creationflags"] = _no_window_flags()
    return subprocess.Popen(cmd, **kw)


def _ffmpeg_option_ok(name, value):
    # True when this ffmpeg accepts the option. ffmpeg parses argv before
    # it requires an output file. A removed option exits with
    # "Unrecognized option" / "Option not found", which is the CPU open
    # failure from -vsync on ffmpeg 8.
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-" + name, value]
    try:
        if sys.platform.startswith("win"):
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15, creationflags=_no_window_flags())
        else:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    err = (result.stdout + result.stderr).decode("ascii", "replace").lower()
    if "unrecognized option" in err or "option not found" in err:
        return False
    return True


def _probe_output_sync_args():
    # One raw frame per decoded frame. The default (cfr) duplicates frames
    # across timestamp gaps, so a later index is the wrong picture.
    # -vsync 0 is that mode on ffmpeg before 8. ffmpeg 8 removed -vsync;
    # -fps_mode passthrough (since 5.1) is the same mode. No single flag
    # is accepted by both generations, so pick the one this binary has.
    # -fps_mode is an output option and must stay after -i.
    if _ffmpeg_option_ok("fps_mode", "passthrough"):
        return ("-fps_mode", "passthrough")
    if _ffmpeg_option_ok("vsync", "0"):
        return ("-vsync", "0")
    return ()


def _output_sync_args():
    global _SYNC_ARGS
    args = _SYNC_ARGS
    if args is None:
        args = _probe_output_sync_args()
        _SYNC_ARGS = args
    return list(args)


def _probe_size(path):
    # (width, height) of stream V:0, or None. Timeout so a stuck ffprobe
    # cannot hang the render open.
    cmd = ["ffprobe", "-v", "error", "-select_streams", "V:0",
           "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", path]
    try:
        if sys.platform.startswith("win"):
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20, creationflags=_no_window_flags())
        else:
            result = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.decode("ascii", "replace").strip().splitlines()
    if not lines or "x" not in lines[0]:
        return None
    w_s, h_s = lines[0].strip().split("x", 1)[:2]
    w_s = w_s.strip()
    h_s = h_s.strip().split(",")[0].strip()
    if not w_s.isdigit() or not h_s.isdigit():
        return None
    w, h = int(w_s), int(h_s)
    if w <= 0 or h <= 0:
        return None
    return w, h


def _drain_stderr(proc, chunks):
    # Read stderr on the side so a chatty ffmpeg cannot fill the pipe and
    # stall the frame read.
    try:
        stream = proc.stderr
        if stream is None:
            return
        while True:
            data = stream.read(4096)
            if not data:
                break
            if sum(len(c) for c in chunks) < 65536:
                chunks.append(data)
    except Exception:
        pass


class _CpuFrame(object):
    # The fields the blend kernel and the icon grab read off a
    # SimpleDecoder frame: shape (h, w, 3), byte strides, device pointer.
    def __init__(self, ptr, width, height, stride):
        self._ptr = int(ptr)
        self.shape = (int(height), int(width), 3)
        self.strides = (int(stride), 3, 1)

    def GetPtrToPlane(self, plane):
        return self._ptr


class CpuDecoder(object):
    # One footage file. seek_to_index / get_batch_frames match the NVDEC
    # decoder the player already calls. Frames stay valid until the next
    # get_batch_frames on this decoder (same as SimpleDecoder's pool).
    # upload(raw, slot) -> pointer, when given, replaces the CUDA upload
    # (tests). Production leaves it None.

    cpu_fallback = True

    def __init__(self, path, upload=None):
        self.path = path
        self._upload = upload
        self._proc = None
        self._err_thread = None
        self._err_chunks = []
        self._primed = None
        self._next = 0
        self._pool = []
        self._closed = False
        self.width = 0
        self.height = 0
        self.stride = 0
        self.nbytes = 0
        try:
            self._open()
        except Exception:
            self.close()
            raise

    def _open(self):
        if not self.path or not os.path.isfile(self.path):
            raise footage_decode_error(self.path, "file not found")
        size = _probe_size(self.path)
        if not size:
            raise footage_decode_error(self.path, "ffprobe found no video stream")
        self.width, self.height = size
        self.stride = self.width * 3
        self.nbytes = self.stride * self.height
        if self.nbytes <= 0:
            raise footage_decode_error(self.path, "empty video frame")
        self._restart(0)
        raw = self._read_exact()
        if raw is None:
            detail = self._err_text() or "ffmpeg produced no frame"
            raise footage_decode_error(self.path, detail)
        self._primed = raw

    def _ffmpeg_cmd(self, index):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", self.path, "-map", "0:V:0", "-an", "-sn", "-dn"]
        if index > 0:
            # Frame-accurate: n is the decoded frame index. Frames before
            # index are dropped inside ffmpeg; the pipe then yields index,
            # index + 1, ... in order.
            cmd += ["-vf", "select=gte(n\\,%d),format=rgb24" % int(index)]
        # BUILD 343: passthrough, spelled so this ffmpeg accepts it.
        cmd += _output_sync_args()
        cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        return cmd

    def _start_proc(self, index):
        self._err_chunks = []
        try:
            self._proc = _popen(
                self._ffmpeg_cmd(index),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        except OSError as exc:
            raise footage_decode_error(self.path, "ffmpeg: %s" % exc)
        proc = self._proc
        chunks = self._err_chunks
        self._err_thread = threading.Thread(
            target=_drain_stderr, args=(proc, chunks), daemon=True)
        self._err_thread.start()

    def _stop_proc(self):
        proc = self._proc
        th = self._err_thread
        self._proc = None
        self._err_thread = None
        if proc is not None:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        if th is not None:
            th.join(timeout=2)

    def _restart(self, index):
        self._stop_proc()
        self._primed = None
        self._next = int(index)
        self._start_proc(self._next)

    def _err_text(self):
        parts = getattr(self, "_err_chunks", None) or []
        try:
            text = b"".join(parts).decode("ascii", "replace").strip()
        except Exception:
            text = ""
        return _one_line(text, 240)

    def _read_exact(self):
        # One rgb24 frame, or None at a clean end. A decode error raises
        # and names this footage path. Advances _next on success.
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        need = self.nbytes
        buf = bytearray()
        try:
            while len(buf) < need:
                chunk = proc.stdout.read(need - len(buf))
                if not chunk:
                    break
                buf.extend(chunk)
        except Exception as exc:
            raise footage_decode_error(self.path, exc)
        if len(buf) == need:
            self._next += 1
            return bytes(buf)
        if len(buf) > 0:
            raise footage_decode_error(self.path, "short frame from ffmpeg")
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = proc.poll()
        th = self._err_thread
        if th is not None:
            th.join(timeout=2)
        err = self._err_text()
        # Exit 0 is a clean end of file, even if stderr mentioned an error
        # while earlier frames still decoded. A non-zero exit is a failure
        # and names this footage. A process that never exits is treated as
        # the end here; the open path reports that as no frame.
        if code not in (0, None):
            raise footage_decode_error(self.path, err or ("ffmpeg exit %s" % code))
        return None

    def seek_to_index(self, index):
        # Leave this decoder holding `index` (primed) so the next
        # get_batch_frames starts there. Past the end raises IndexError,
        # which the player already treats as a skipped layer and remembers
        # so later frames of this file are not decoded.
        index = int(index)
        if self._closed:
            raise footage_decode_error(self.path, "decoder is closed")
        if index < 0:
            raise IndexError("Index %d is out of range" % index)
        if self._primed is not None and index == self._next - 1:
            return
        # Short forward step on the pipe already open for this file.
        # A jump backward, or a long jump, starts ffmpeg at that index.
        if not (self._proc is not None and index >= self._next
                and index - self._next <= 8):
            self._restart(index)
        else:
            self._primed = None
            while self._next < index:
                if self._read_exact() is None:
                    raise IndexError("Index %d is out of range" % index)
        raw = self._read_exact()
        if raw is None:
            raise IndexError("Index %d is out of range" % index)
        self._primed = raw

    def _take(self):
        if self._primed is not None:
            raw = self._primed
            self._primed = None
            return raw
        return self._read_exact()

    def get_batch_frames(self, count):
        if self._closed:
            raise footage_decode_error(self.path, "decoder is closed")
        count = int(count)
        if count <= 0:
            return []
        frames = []
        for i in range(count):
            raw = self._take()
            if raw is None:
                if i == 0:
                    raise IndexError("Index %d is out of range" % self._next)
                break
            try:
                ptr = self._upload_slot(raw, i)
            except Exception as exc:
                raise footage_decode_error(self.path, exc)
            frames.append(_CpuFrame(ptr, self.width, self.height, self.stride))
        return frames

    def _upload_slot(self, raw, slot):
        if self._upload is not None:
            return int(self._upload(raw, slot))
        return self._cuda_upload(raw, slot)

    def _cuda_upload(self, raw, slot):
        # Device RGB in the shared CUDA context the player and the render
        # composer already use. Synchronous copy: the blend kernel is
        # launched only after this returns.
        while len(self._pool) <= slot:
            self._pool.append(self._cuda_alloc())
        ptr = self._pool[slot]
        buf = ctypes.create_string_buffer(raw)
        from sequence_player_gpu import _check
        _check(self._cudart.cudaMemcpy(ptr, buf, len(raw), 1), "cpu decode copy")
        return int(ptr.value)

    def _cuda_alloc(self):
        from cuda_context import ensure_current
        from sequence_player_gpu import _check, _libs
        ensure_current()
        cudart = _libs()[0]
        self._cudart = cudart
        ptr = ctypes.c_void_p()
        _check(cudart.cudaMalloc(ctypes.byref(ptr), self.nbytes), "cpu decode malloc")
        return ptr

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._primed = None
        try:
            self._stop_proc()
        except Exception:
            pass
        pool = self._pool
        self._pool = []
        if not pool:
            return
        try:
            from cuda_context import ensure_current
            from sequence_player_gpu import _libs
            ensure_current()
            cudart = _libs()[0]
            for ptr in pool:
                try:
                    cudart.cudaFree(ptr)
                except Exception:
                    pass
        except Exception:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

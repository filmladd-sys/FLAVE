# cuda_context.py
# One shared CUDA primary context for the whole process, created at launch.
# The Sequence Player (GL interop) and the preview worker (nvJPEG) both
# attach to it. ASCII only.

import ctypes
import threading

# cudaErrorSetOnActiveProcess: cudaGLSetGLDevice after a context exists
CUDA_ERROR_SET_ON_ACTIVE_PROCESS = 708

_LOCK = threading.Lock()
_READY = False
_DEVICE = 0
_CTX = ctypes.c_void_p()


def _bind_extra(cudart, nvcuda):
    # APIs needed for shared-context init; safe to set more than once.
    c = ctypes
    if not getattr(cudart, "_ladd_ctx_bound", False):
        cudart.cudaSetDevice.argtypes = [c.c_int]
        cudart.cudaSetDevice.restype = c.c_int
        cudart.cudaGetDevice.argtypes = [c.POINTER(c.c_int)]
        cudart.cudaGetDevice.restype = c.c_int
        cudart.cudaGetLastError.argtypes = []
        cudart.cudaGetLastError.restype = c.c_int
        cudart.cudaPeekAtLastError.argtypes = []
        cudart.cudaPeekAtLastError.restype = c.c_int
        cudart._ladd_ctx_bound = True
    if not getattr(nvcuda, "_ladd_ctx_bound", False):
        # Already bound in _libs for Get/SetCurrent; keep a flag for symmetry
        nvcuda._ladd_ctx_bound = True


def ensure_cuda(device=0):
    # Create (once) the primary context on device and make it current.
    # Returns the CUcontext pointer (ctypes.c_void_p). Never destroys it.
    global _READY, _DEVICE, _CTX
    with _LOCK:
        if _READY and _CTX.value:
            _ensure_current_locked()
            return _CTX
        from sequence_player_gpu import _libs, _check
        cudart, nvcuda, _gl = _libs()
        _bind_extra(cudart, nvcuda)
        _DEVICE = int(device)
        err = cudart.cudaSetDevice(_DEVICE)
        if err != 0:
            raise RuntimeError("cudaSetDevice failed: %s" % err)
        # Establish the primary context on this device
        err = cudart.cudaFree(None)
        if err != 0:
            raise RuntimeError("cudaFree(None) init failed: %s" % err)
        cudart.cudaGetLastError()  # clear any sticky leftover
        ctx = ctypes.c_void_p()
        err = nvcuda.cuCtxGetCurrent(ctypes.byref(ctx))
        if err != 0 or not ctx.value:
            raise RuntimeError("cuCtxGetCurrent failed: %s" % err)
        _CTX = ctx
        _READY = True
        return _CTX


def _ensure_current_locked():
    if not _CTX.value:
        return
    from sequence_player_gpu import _libs
    _cudart, nvcuda, _gl = _libs()
    err = nvcuda.cuCtxSetCurrent(_CTX)
    if err != 0:
        raise RuntimeError("cuCtxSetCurrent failed: %s" % err)


def ensure_current():
    # Make the shared context current on this thread. Creates it if needed.
    ensure_cuda(_DEVICE)
    with _LOCK:
        _ensure_current_locked()
    return _CTX


def get_context():
    # Shared CUcontext pointer; creates the context if needed.
    return ensure_cuda(_DEVICE)


def soft_cuda_gl_set_device(device=0):
    # Call cudaGLSetGLDevice without aborting on 708. Clears sticky errors.
    # Returns the raw cudart error code.
    from sequence_player_gpu import _libs
    cudart, _nvcuda, _gl = _libs()
    _bind_extra(cudart, _nvcuda)
    err = cudart.cudaGLSetGLDevice(int(device))
    # 0 = success; 708 = already set for this process / active context
    # if err not in (0, CUDA_ERROR_SET_ON_ACTIVE_PROCESS):
    #     print("cudaGLSetGLDevice soft: %s" % err)
    cudart.cudaGetLastError()
    return err

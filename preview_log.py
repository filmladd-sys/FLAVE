# BUILD 247: console timing of preview generation startup. Lines look like
#   [preview] +0.123s <step> (dt 0.045s)
# +: seconds since the session start (an F5 refresh request, or the start
# of a full worker pass: refresh or Play fill); dt: seconds since the
# previous line. A session prints its steps (snapshot, refresh plan, first
# prefetch, decoder opens, first frame per decoder, first composite, first
# commit), a progress line about every PROGRESS_SEC and a summary at the
# end of the pass. Outside a session only decoder opens and audio mix
# start / end print (time since the previous line). PREVIEW_LOG follows
# RENDER_LOG. Timing only: no behavior depends on it. ASCII only.

import os
import threading
import time

# RENDER_LOG: the one switch for the verbose render / preview console
# instrumentation: these [preview] lines, the WORKER refresh states /
# decoders / plan / range / pool / batch lines, DECODER open / close and
# RENDER decoders. False (default): silent; errors, warnings, the VRAM
# fatal stop and the one-line pass / render summaries still print. Set
# True to re-enable the instrumentation.
RENDER_LOG = False
PREVIEW_LOG = RENDER_LOG
PROGRESS_SEC = 2.0

_LOCK = threading.Lock()
_S = {"t0": None, "last": None, "kind": "", "once": set(), "dec_keys": set(),
      "opens": 0, "open_s": 0.0, "frames": 0, "total": 0, "prog_t": 0.0,
      "pass_t0": None}


def _now():
    return time.perf_counter()


def log(step):
    # One timestamped line (no-op when PREVIEW_LOG is False)
    if not PREVIEW_LOG:
        return
    with _LOCK:
        now = _now()
        t0 = _S["t0"]
        last = _S["last"] if _S["last"] is not None else now
        _S["last"] = now
        if t0 is None:
            line = "[preview] %s (dt %.3fs)" % (step, now - last)
        else:
            line = "[preview] +%.3fs %s (dt %.3fs)" % (now - t0, step, now - last)
    print(line)


def active():
    return PREVIEW_LOG and _S["t0"] is not None


def begin(kind, detail=""):
    # Start a session (F5 request or a full pass). A session already open
    # (F5 requested, its pass starting now) keeps its start time.
    if not PREVIEW_LOG:
        return
    with _LOCK:
        fresh = _S["t0"] is None
        if fresh:
            now = _now()
            _S.update(t0=now, last=now, kind=kind, once=set(), dec_keys=set(),
                      opens=0, open_s=0.0, frames=0, total=0, prog_t=now,
                      pass_t0=None)
    log("%s %s%s" % ("start" if fresh else "pass", kind,
                     (": " + detail) if detail else ""))


def pass_started(total):
    # The worker pass that generates the frames began (total = frames to do)
    if not active():
        return
    with _LOCK:
        _S["pass_t0"] = _now()
        _S["total"] = int(total)
        _S["frames"] = 0
        _S["prog_t"] = _S["pass_t0"]


def once(tag, step=None):
    # Log step the first time tag is seen in this session
    if not active():
        return
    with _LOCK:
        if tag in _S["once"]:
            return
        _S["once"].add(tag)
    log(step or tag)


def decoder_opened(path, sec):
    if not PREVIEW_LOG:
        return
    with _LOCK:
        _S["opens"] += 1
        _S["open_s"] += sec
    log("decoder open %s %.0f ms" % (os.path.basename(str(path)), sec * 1000.0))


def first_decode(key, n):
    # First frame(s) a decoder returned in this session
    if not active():
        return
    with _LOCK:
        if key in _S["dec_keys"]:
            return
        _S["dec_keys"].add(key)
    log("first frame decoded %s (%d in batch)" % (
        os.path.basename(str(key[1] if isinstance(key, tuple) else key)), n))


def frames_done(n):
    # n frames generated; a progress line about every PROGRESS_SEC
    if not active():
        return
    with _LOCK:
        _S["frames"] += int(n)
        now = _now()
        due = now - _S["prog_t"] >= PROGRESS_SEC
        if due:
            _S["prog_t"] = now
        p0 = _S["pass_t0"] or _S["t0"]
        done, total = _S["frames"], _S["total"]
    if due:
        el = now - p0
        log("progress %d/%d frames, %.1f fps" % (done, total,
                                                 done / el if el > 0 else 0.0))


def end(reason="done"):
    # Summary line, then the session closes
    if not active():
        return
    with _LOCK:
        now = _now()
        t0, p0 = _S["t0"], _S["pass_t0"] or _S["t0"]
        frames, opens, open_s, kind = _S["frames"], _S["opens"], _S["open_s"], _S["kind"]
    el = now - p0
    log("summary %s %s: total %.3fs, %d frames, %.1f fps, %d decoder opens "
        "%.3fs" % (kind, reason, now - t0, frames,
                   frames / el if el > 0 and frames else 0.0, opens, open_s))
    with _LOCK:
        _S["t0"] = None

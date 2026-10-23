# sequence_player_preview.py
# JPEG preview path for the Sequence Player: one frame and a card batch.
# Methods take the SequenceTimeline player as self and are attached by
# sequence_player.py (and borrowed by sequence_preview_gpu's
# HeadlessComposer). ASCII only.
#
# One frame engine: the preview worker composites every frame on the
# headless composer (sequence_preview_gpu), whether or not a Sequence
# Player is open; the player only presents (preview.db JPEGs, play ring)
# and keeps no decoders. The open player composites only as a fallback
# when the engine's GPU setup failed. The render runs the same
# composite_batch on a private composer of the same class (own decoders,
# originals).
#
# The explicit refresh (F5) and its VRAM budgeting also live here; the
# memory model is explained in the block comment above
# _composite_by_layer.

import ctypes
import os
import sys
import time

import preview_log      # RENDER_LOG: verbose console instrumentation switch
from PySide6.QtCore import QBuffer, QIODevice, Qt
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
        img = _qt_preview_image(self, data)
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
            img = _qt_preview_image(self, data)
            buf = QBuffer()
            buf.open(QIODevice.WriteOnly)
            out.append(bytes(buf.data()) if img.save(buf, "JPG", quality) else None)
        except Exception as e:
            print(f"Sequence Player frame_jpeg failed: {e}")
            out.append(None)
    return out

def _qt_preview_image(self, data):
    # Qt fallback (no nvJPEG): the host RGB composite as a QImage at the
    # project's Preview Size (smooth scale when that is not 100%)
    img = QImage(data, self.seq_w, self.seq_h, self.row, QImage.Format_RGB888)
    pw, ph = _preview_dims(self)
    if (pw, ph) != (self.seq_w, self.seq_h):
        img = img.scaled(pw, ph, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return img


# ---------------------------------------------------------------------
# Preview Size (BUILD 255, Project Settings > Project). Frames are always
# composited at the full sequence size (every layer's scale, position,
# crop, rotate and outline exactly as at 100%). Below 100% each composite
# is then scaled down on the card (resample_rgb) into a small buffer and
# nvJPEG encodes that, so only the smaller JPEG leaves the card and goes
# to disk. The player scales any stored size back to the sequence size
# (decode_play_jpegs for playback, QImage.scaled for a still frame).

def _preview_pct(self):
    # BUILD 320: this composer's sequence Preview Size percent (the sequence
    # override, else the project Preview Size)
    from sequence_preview import sequence_preview_size_percent
    sid = getattr(self, "sequence_id", None)
    if sid is None:
        sid = getattr(getattr(self, "timeline", None), "sequence_id", None)
    try:
        return sequence_preview_size_percent(self.timeline._project_db(), sid)
    except Exception:
        return 100


def _preview_dims(self):
    # (w, h) the preview JPEGs are stored at for this composer's sequence
    from sequence_preview import preview_frame_size
    return preview_frame_size(self.seq_w, self.seq_h, _preview_pct(self))


def resample_rgb(self, src, sw, sh, sstride, dst, dw, dh):
    # Scale an RGB device image (sw x sh, row stride sstride) into dst (dw x
    # dh, rows dw * 3 bytes) on self.stream, nearest pixel, no sync. Runs
    # the blend kernel as a plain copy: 3 bytes per pixel (opaque),
    # opacity 1, no crop, color, rotate or outline, so every dst pixel is
    # exactly one source pixel (the blend with alpha 255 keeps none of
    # dst). Sampling is at pixel centers: ox / oy center the source the
    # same way _blend does, inv_scale = sw / dw per axis.
    nvcuda = _libs()[1]
    c = ctypes
    ptrs = [c.c_uint64(int(src.value)), c.c_uint64(int(dst.value))]
    ptrs += [c.c_uint32(int(v)) for v in (sw, sh, sstride, 3, dw, dh)]
    ptrs += [c.c_int32((int(sw) - int(dw)) // 2), c.c_int32((int(sh) - int(dh)) // 2),
             c.c_float(float(sw) / float(dw)), c.c_int32(0), c.c_int32(0),
             c.c_float(float(sh) / float(dh)), c.c_float(1.0),
             c.c_float(0.0), c.c_float(0.0), c.c_uint32(0),
             c.c_float(1.0), c.c_float(0.0), c.c_float(1.0),
             c.c_float(1.0), c.c_float(0.0), c.c_float(0.0), c.c_uint32(0)]
    args = (c.c_void_p * len(ptrs))(
        *[c.cast(c.pointer(v), c.c_void_p) for v in ptrs])
    grid = (int(dw) * int(dh) + 255) // 256
    _check(nvcuda.cuLaunchKernel(self.kernel, grid, 1, 1, 256, 1, 1, 0,
                                 self.stream, args, None), "resample")


def _scaled_buffers(self, n, pw, ph):
    # n Preview Size RGB device buffers (pw x ph, rows pw * 3), kept and
    # grown up to the largest n asked (at most the encoder's max_states:
    # encode_preview works group by group); all freed when the size
    # changes. Lock held.
    if getattr(self, "_scaled_dims", None) != (pw, ph):
        _free_scaled(self)
        self._scaled_dims = (pw, ph)
    cudart = _libs()[0]
    while len(self._scaled) < n:
        ptr = ctypes.c_void_p()
        _check(cudart.cudaMalloc(ctypes.byref(ptr), pw * 3 * ph),
               "preview size buffer malloc")
        self._scaled.append(ptr)
    return self._scaled[:n]


def _free_scaled(self):
    # Free the Preview Size buffers (composer / player release, size change)
    cudart = _libs()[0]
    for b in getattr(self, "_scaled", None) or []:
        if b.value:
            cudart.cudaFree(b)
    self._scaled = []
    self._scaled_dims = None


def encode_preview(self, bufs, quality):
    # nvJPEG bytes of full-size composites bufs, at the sequence's Preview
    # Size (BUILD 320: its override, else the project's). At 100% this is
    # encode_many on the composites; below it, groups
    # of max_states are scaled into the small buffers and encoded from
    # there (encode_many syncs the stream, so the next group can reuse
    # them). Lock held, self.nvj created.
    # BUILD 317: the encoder's max_states is sized for this encode size
    # first (nvj_fit_states), on every pass (normal and refresh).
    pw, ph = _preview_dims(self)
    nvj_fit_states(self, pw, ph)
    if (pw, ph) == (self.seq_w, self.seq_h):
        return self.nvj.encode_many(bufs, self.seq_w, self.seq_h, self.row, quality)
    out = []
    step = self.nvj.max_states
    for g0 in range(0, len(bufs), step):
        group = bufs[g0:g0 + step]
        small = _scaled_buffers(self, len(group), pw, ph)
        for b, d in zip(group, small):
            resample_rgb(self, b, self.seq_w, self.seq_h, self.row, d, pw, ph)
        out.extend(self.nvj.encode_many(small, pw, ph, pw * 3, quality))
    return out


# BUILD 317: nvJPEG encoder states per encoder, by resolution. A warmed
# state costs about 60 MB at 1920 x 1080 and scales with the encoded
# pixels (about 240 MB at 3840 x 2160, about 615 MB at 6144 x 3456), so a
# fixed 16 states could take most of the card at 6K. max_states = the
# states that fit in NVJ_STATES_VRAM_SHARE of total VRAM at the measured
# (refresh_plan) or estimated (NVJ_STATE_BYTES_PX) cost of one state at
# the encode size, clamped to 1..STATES_MAX (16).
NVJ_STATES_VRAM_SHARE = 0.20
NVJ_STATE_BYTES_PX = (60 << 20) / float(1920 * 1080)


def _nvj_states_for(total_b, cost_b):
    # States that fit in NVJ_STATES_VRAM_SHARE of total_b at cost_b bytes
    # per state, 1..STATES_MAX (STATES_MAX when either is unknown)
    if not total_b or not cost_b or cost_b <= 0:
        return _NvJpegEncoder.STATES_MAX
    return max(1, min(_NvJpegEncoder.STATES_MAX,
                      int((NVJ_STATES_VRAM_SHARE * total_b) // cost_b)))


def nvj_fit_states(self, pw, ph, cost=None):
    # (lock held) Set self.nvj.max_states for encodes at pw x ph. cost =
    # measured bytes of one warmed state (refresh_plan); None keeps a
    # cost already known for this size, else uses the estimate. Kept in
    # self._nvj_cost = ((pw, ph), cost, max_states, measured). Returns
    # max_states (None without an encoder).
    nvj = getattr(self, "nvj", None)
    if nvj is None:
        return None
    known = getattr(self, "_nvj_cost", None)
    measured = cost is not None
    if cost is None:
        if known is not None and known[0] == (pw, ph):
            if nvj.max_states != known[2]:
                nvj.set_max_states(known[2])
            return known[2]
        cost = int(NVJ_STATE_BYTES_PX * int(pw) * int(ph))
    total = getattr(self, "_vram_total", None)
    if not total:
        fv = free_vram()
        total = fv[1] if fv else 0
        self._vram_total = total
    m = nvj.set_max_states(_nvj_states_for(total, cost))
    self._nvj_cost = ((pw, ph), int(cost), m, measured)
    return m


def nvj_states_cap(self):
    # The encoder's max_states (STATES_MAX without an encoder)
    nvj = getattr(self, "nvj", None)
    return nvj.max_states if nvj is not None else _NvJpegEncoder.STATES_MAX


# Play scratch: device buffers a non-sequence-size JPEG decodes into before
# decode_play_jpegs scales it into its play buffer (reader thread, under
# the player's _play_lock)
PLAY_SCRATCH_GROUP = 16


def _play_scratch(self, n, nbytes):
    # n scratch buffers of at least nbytes each (kept, regrown when larger)
    cudart = _libs()[0]
    if getattr(self, "_play_scratch_bytes", 0) < nbytes:
        _free_play_scratch(self)
        self._play_scratch_bytes = int(nbytes)
    while len(self._play_scratch) < n:
        ptr = ctypes.c_void_p()
        _check(cudart.cudaMalloc(ctypes.byref(ptr), self._play_scratch_bytes),
               "play scratch malloc")
        self._play_scratch.append(ptr)
    return self._play_scratch[:n]


def _free_play_scratch(self):
    # Free the play scratch buffers (player release, regrow)
    cudart = _libs()[0]
    for b in getattr(self, "_play_scratch", None) or []:
        if b.value:
            cudart.cudaFree(b)
    self._play_scratch = []
    self._play_scratch_bytes = 0


def decode_play_jpegs(self, dec, jpegs, bufs):
    # Player reader thread, _play_lock held: each JPEG into its
    # sequence-size play buffer (rows self.row). Sequence-size JPEGs (and
    # any whose header cannot be read) decode straight in, one batch.
    # Any other size (a Preview Size below 100%, or frames stored before a
    # size change) decodes into scratch buffers, groups of
    # PLAY_SCRATCH_GROUP, and is scaled to the sequence size on the card
    # (resample_rgb), so the texture, its copy and the present always get
    # a sequence-size frame.
    from sequence_preview import jpeg_size
    full = (self.seq_w, self.seq_h)
    direct, other = [], []
    for jpeg, buf in zip(jpegs, bufs):
        size = jpeg_size(jpeg)
        if size is None or size == full:
            direct.append((jpeg, buf))
        else:
            other.append((jpeg, buf, size))
    if direct:
        dec.decode_many([j for j, _b in direct], [b for _j, b in direct], self.row)
    cudart = _libs()[0]
    for g0 in range(0, len(other), PLAY_SCRATCH_GROUP):
        group = other[g0:g0 + PLAY_SCRATCH_GROUP]
        pitch = max(s[0] for _j, _b, s in group) * 3
        rows = max(s[1] for _j, _b, s in group)
        scratch = _play_scratch(self, len(group), pitch * rows)
        dec.decode_many([j for j, _b, _s in group], scratch, pitch)
        for (_j, buf, (w, h)), src in zip(group, scratch):
            resample_rgb(self, src, w, h, pitch, buf, self.seq_w, self.seq_h)
        _check(cudart.cudaStreamSynchronize(self.stream), "play resample sync")
    return True


def _prefetch(self, frames, snapshot, clips=None):
    # {((instance, path), src_frame): decoded frame} per decoder key
    # (layer item [16], one decoder per file instance) whose source frames
    # run consecutively across the batch. Callers split a batch where a
    # key's source jumps (split_runs), so consecutive clips of one file
    # (Scene Detect pieces) decode as one run on one decoder.
    # clips (set of clip ids, or None for all): only those clips' layers
    # (the explicit refresh decodes one track at a time)
    reqs = {}
    last = {}
    import preview_log      # BUILD 247
    t_pre = time.perf_counter()
    if frames:
        self._prune_decoders(frames[0], snapshot)
    for fr in frames:
        for item in self._layer_items(fr, snapshot):
            kind, path, src = item[0], item[1], item[2]
            if clips is not None and item[6][0] not in clips:
                continue
            if kind == "video" and os.path.isfile(path):
                key = item[16] if len(item) > 16 else (0, path)
                reqs.setdefault(key, []).append(src)
                last[key] = fr
    pre = {}
    for key, srcs in reqs.items():
        if len(srcs) < 2 or srcs != list(range(srcs[0], srcs[0] + len(srcs))):
            continue
        dec = self._decoder(key, last.get(key))
        # BUILD 240: only the part of the run not already known to be past
        # the source end (BUILD 241: known only from an earlier refused
        # seek); _build_dst skips those layers
        from sequence_player import (source_index_ok, is_out_of_range_error,
                                     learn_source_end)
        srcs = [s for s in srcs if source_index_ok(self, key, dec, s)]
        if not srcs:
            continue
        if self.dec_next.get(key) != srcs[0]:
            try:
                dec.seek_to_index(srcs[0])
            except Exception as e:
                self.dec_next.pop(key, None)
                if is_out_of_range_error(e):
                    learn_source_end(key, srcs[0])     # BUILD 241
                    continue    # decoded per frame (and skipped) in _build_dst
                raise
        try:
            got = dec.get_batch_frames(len(srcs))
        except Exception as e:
            self.dec_next.pop(key, None)
            if is_out_of_range_error(e):
                continue        # decoded per frame (and skipped) in _build_dst
            raise
        got = list(got or [])
        if got:
            preview_log.first_decode(key, len(got))     # BUILD 247
        for k, f in enumerate(got):
            pre[(key, srcs[0] + k)] = f
        if got:
            self.dec_next[key] = srcs[0] + len(got)
        else:
            self.dec_next.pop(key, None)
    preview_log.once("first prefetch", "first prefetch: %d frames, %d decoded "
                     "(%.3fs)" % (len(frames), len(pre), time.perf_counter() - t_pre))
    return pre

def split_runs(self, frames, snapshot, clips=None):
    # frames cut into runs (lists) so that no decoder key's source jumps
    # inside a run: a cut goes before a frame where a key changes to
    # another clip whose source frame is not the one that key decodes
    # next (a backward or forward jump between clips of one file).
    # Within one clip nothing is cut (repeats and skips of a frame-mapped
    # proxy decode per frame in _build_dst, as before). One decoder
    # cannot hold two runs' frames at once (a later get_batch_frames
    # hands earlier frames back), so each run is decoded, blended and
    # synced before the next. clips: as in _prefetch.
    runs = []
    cur = []
    nxt = {}        # key -> (clip_id, source frame decoded next)
    for fr in frames:
        layers = []
        cut = False
        for item in self._layer_items(fr, snapshot):
            if item[0] != "video" or (clips is not None and item[6][0] not in clips):
                continue
            key = item[16] if len(item) > 16 else (0, item[1])
            layers.append((key, item[6][0], item[2]))
            seen = nxt.get(key)
            if seen is not None and seen[0] != item[6][0] and seen[1] != item[2]:
                cut = True
        if cut and cur:
            runs.append(cur)
            cur = []
            nxt = {}
        cur.append(fr)
        for key, clip_id, src in layers:
            nxt[key] = (clip_id, src + 1)
    if cur:
        runs.append(cur)
    return runs


def decoder_keys_text(self, snapshot):
    # Console text: the distinct decoder keys of a snapshot (file and
    # instance, with its clip count), e.g. for the refresh and render
    # start lines
    inst = self._dec_instances(snapshot)
    count = {}
    for key in inst.values():
        count[key] = count.get(key, 0) + 1
    keys = sorted(count, key=lambda k: (str(k[1]), int(k[0])))
    shown = ["%s #%d (%d clips)" % (k[1], int(k[0]), count[k]) for k in keys[:20]]
    if len(keys) > 20:
        shown.append("... %d more" % (len(keys) - 20))
    files = len(set(k[1] for k in keys))
    return "%d decoder keys, %d files: %s" % (
        len(keys), files, "; ".join(shown) if shown else "none")


# Device memory one decoder is assumed to hold for decoder_capacity: its
# largest source's REFRESH_DECODE_BYTES_PX x w x h per frame, times the
# pull size (gen._pull_k), or DEC_EST_FRAMES when there is none
DEC_EST_FRAMES = 8


def decoder_capacity(gen, live):
    # How many decoders may stay open (sequence_player._prune_decoders):
    # the ones open now plus as many more of the largest live source as
    # fit in free VRAM above the refresh keep (_refresh_keep), or minus
    # as many as free VRAM is short of the keep, between 1 and DEC_MAX.
    # Open decoders are already out of free VRAM. DEC_MAX when free VRAM
    # cannot be read.
    from sequence_player import DEC_MAX
    fv = free_vram()
    if fv is None:
        return DEC_MAX
    pull = int(getattr(gen, "_pull_k", 0) or 0) or DEC_EST_FRAMES
    big = 0
    for key in live:
        size = _source_size(key[1]) or (int(gen.seq_w), int(gen.seq_h))
        big = max(big, size[0] * size[1])
    est = int(big * REFRESH_DECODE_BYTES_PX * pull)
    room = fv[0] - _refresh_keep(fv[1])
    more = int(room // est) if est > 0 else 0
    return max(1, min(DEC_MAX, len(gen.decoders) + more))


# Composite buffers / nvJPEG states a composer keeps after a normal pass.
# A pass of more than GEN_KEEP frames frees what it grew beyond that, so
# the next free-VRAM query sees the real free memory. While an explicit
# refresh holds its pool (_refresh_hold) nothing is trimmed.
GEN_KEEP = 30


def _trim_gen(self):
    # Free composite buffers / nvJPEG states beyond GEN_KEEP (lock held).
    # Never while a refresh pool is reserved: the refresh sized it.
    if getattr(self, "_refresh_hold", 0):
        return
    if len(self.gen_bufs) > GEN_KEEP:
        cudart = _libs()[0]
        for b in self.gen_bufs[GEN_KEEP:]:
            if b.value:
                cudart.cudaFree(b)
        del self.gen_bufs[GEN_KEEP:]
    if self.nvj is not None:
        self.nvj.trim(GEN_KEEP)


def _gen_buffers(self, n):
    # n sequence-size RGB device buffers for one GPU pass (grown, kept)
    cudart = _libs()[0]
    if n <= GEN_KEEP:
        _trim_gen(self)
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
    # The pass itself is composite_batch (shared with the render); this
    # is its preview JPEG sink.
    if self.closed or not self.ready:
        return None
    n = len(frames)
    try:
        items = composite_batch(self, frames, snapshot, _dark_mode(),
                                self._gen_buffers(n), _jpeg_sink(quality))
        self._target = None
        if n > GEN_KEEP:
            # a large pass gives its extra memory back (no-op while a
            # refresh pool is held, see _trim_gen)
            _trim_gen(self)
    except Exception as e:
        self._target = None
        print(f"Sequence Player frame_jpeg failed: {e}")
        items = [("jpeg", None)] * n
    return items


def composite_batch(self, frames, snapshot, dark, bufs, sink,
                    sync_name="composite sync"):
    # The one frame-batch loop of the preview fill and the render. Caller
    # holds _GEN_LOCK. Decode (one get_batch_frames per in-order source),
    # composite every frame into its device buffer (stream ordered, no
    # host sync between frames), one stream sync, then sink(self, bufs)
    # turns the N device buffers into the result list.
    # self: a SequencePlayer or a HeadlessComposer. Raises on CUDA errors.
    cudart = _libs()[0]
    nvcuda = _libs()[1]
    self._tm = _new_tm()
    _check(nvcuda.cuCtxSetCurrent(self.cuda_ctx), "ctx")
    if getattr(self, "_refresh_hold", 0) and len(frames) > 1:
        # explicit refresh (pool reserved): composite track by track
        _composite_by_layer(self, frames, snapshot, dark, bufs)
        out = sink(self, bufs)
        return out
    # 1) decode, per run (split_runs: no decoder's source jumps inside a
    # run; usually the whole batch is one run)
    # 2) composite the run's frames on the card (prefetched blends are
    # stream ordered, no host sync between frames); a run is synced
    # before the next run decodes on the same decoders
    k = 0
    for run in split_runs(self, frames, snapshot):
        pre = self._prefetch(run, snapshot)
        for fr in run:
            self._build_dst(fr, dark, bufs[k], snapshot, False, pre, sync=False)
            k += 1
        _check(cudart.cudaStreamSynchronize(self.stream), sync_name)
        del pre
    # 3) sink: encode / copy out from the device buffers
    out = sink(self, bufs)
    return out


# ---------------------------------------------------------------------
# Explicit refresh (F5 / Refresh Preview): the VRAM memory model
#
# Goal: build every dirty preview frame in big card passes as fast as
# the card allows, without ever running the card out of video memory.
# Only dedicated VRAM (cudaMemGetInfo on the device) is counted; shared
# system memory is never part of the budget.
#
# The keep. _refresh_keep() is the free VRAM the refresh always leaves
# for Windows, the GUI and other programs:
#     max(REFRESH_VRAM_MARGIN (25%) x total VRAM, REFRESH_VRAM_FATAL_MB
#     (1500 MB)) + REFRESH_CHUNK_CUSHION_MB (256 MB)
# e.g. 3070 + 256 = 3326 MB on a 12281 MB card.
#
# The plan. refresh_plan() runs once per refresh pass (the worker reuses
# it when a pass restarts with the same inputs). It cuts the sequence at
# every clip edge on any track, merges neighbouring ranges that decode
# the same clips and files, and gives each range its own N (frames per
# card pass). N comes from this machine's free VRAM and the real width x
# height of each source in the range (ffprobe, proxy or original). The
# Preview Frame Cap setting is only an upper bound on N, and N is never
# more than the range length. A card pass never crosses a range end, so
# no pass mixes two ranges' sources.
#
# What a card pass of N frames holds on the card:
#   - N composite buffers, one per frame, row x height bytes each
#     (sequence-size RGB). These are the "refresh pool".
#   - nvJPEG encoder states: at most the encoder's max_states (BUILD
#     317: 1..16 by resolution, see nvj_fit_states), not
#     one per frame (encode_many reuses them group by group). nvJPEG
#     allocates a state's memory at its first encode, so the plan
#     reserves and warms them (one encode each) BEFORE it measures free
#     VRAM; their real size is then already out of the free number.
#   - Decode: REFRESH_DECODE_BYTES_PX = 4.5 bytes per source pixel per
#     decoded frame per video layer (RGB output frame 3 + NV12 decode
#     surface 1.5).
#   - Stills: RGBA, 4 bytes per pixel, once per still in the range.
#
# Decoders stay open across the batches of a range (no reopen and seek
# per batch) and each video layer is pulled k frames at a time
# (gen._pull_k). A PyNvVideoCodec SimpleDecoder keeps an output pool as
# large as its largest get_batch_frames pull and frees it only when the
# decoder is closed, so the decode held is k x (sum of the layers'
# per-frame decode costs), not N x. k = N when everything fits;
# otherwise N gets the room left after pulls of max_states frames, and k
# gets whatever room N leaves.
#
# Decoders are per file (decoder key (instance, path), see
# sequence_player._dec_instances): refresh_plan closes every decoder once
# at the start of the pass, and a file's decoder then stays open across
# ranges while the ranges use it (seeking in it when the source jumps).
#
# fit_refresh_pool() runs at the start of each range. It closes the
# decoders the range does not use, re-reads the real free VRAM, sizes the pool to the range's N
# and checks the keep again after reserving. On a shortfall it lowers k
# first (never below max_states, or k if that is smaller) and lowers N
# only for what that cannot cover, so N still covers the range. At N = 1
# it frees nvJPEG states next (BUILD 317); if the card is still too
# short (free after decode under REFRESH_VRAM_FATAL_MB +
# REFRESH_CHUNK_CUSHION_MB) the refresh pass is aborted with a message
# (REFRESH_ABORT) instead of running into the hard exit.
#
# Guards. kept_decoder_guard() runs after each batch: if free VRAM is
# under the keep while decoders are held open, it closes them all and
# the rest of the range closes each layer's decoders after its blend.
# vram_fatal_check() is the hard stop: free VRAM under 1500 MB at any
# check point exits FLAVE at once (see that function for why); after
# BUILD 317 that is only reached by drift after a successful reserve.
# ---------------------------------------------------------------------


def _composite_by_layer(self, frames, snapshot, dark, bufs):
    # Explicit refresh: composite the N frames of a batch one track at a
    # time. All N buffers are cleared, then per track, bottom track
    # first: decode that track's frames in pulls of gen._pull_k, blend
    # each pull into its buffers, sync. Every frame still gets its layers
    # blended bottom first, the same image as composite_batch.
    # Decoders (per file instance) this batch does not use are closed
    # before each track (their output pools would otherwise stay
    # allocated). With gen._keep_dec on (set by fit_refresh_pool), the
    # decoders of this batch stay open for the next batch; with it off,
    # each track's decoders are closed as soon as its blend is synced, so
    # only one layer's decode is held at a time. Each pull is split where
    # a decoder's source jumps (split_runs). Stills are freed after their
    # track, and any other still left from earlier batches at the end.
    cudart = _libs()[0]
    for b in bufs:
        _check(cudart.cudaMemsetAsync(b, 0 if dark else 255,
                                      self.row * self.seq_h, self.stream), "clear")
    used = set()
    used_keys = {}      # clip id -> decoder key
    stills = set()
    for fr in frames:
        for item in self._layer_items(fr, snapshot):
            used.add(item[6][0])
            if item[0] in ("image", "text"):      # BUILD 309: text textures
                stills.add(item[1])
            elif len(item) > 16:
                used_keys[item[6][0]] = item[16]
    batch_keys = set(used_keys.values())
    clips_by_track = snapshot[1]
    for track_id, _en, _solo, _kind in reversed(snapshot[0]):
        clips = set(c[0] for c in clips_by_track.get(track_id, [])) & used
        if not clips:
            continue
        track_keys = set(used_keys[c] for c in clips if c in used_keys)
        closed = 0
        # keep_dec: close only decoders this batch does not use; else
        # also every other track's (one layer's decoders at a time)
        keep_dec = getattr(self, "_keep_dec", False)
        for key in list(self.decoders.keys()):
            if key not in (batch_keys if keep_dec else track_keys):
                self._close_decoder(key, "not in this batch" if keep_dec
                                    else "not in this track")
                closed += 1
        # Pull size k (gen._pull_k, 0: the whole batch). A decoder's pool
        # is sized by its largest pull, so open decoders hold k frames
        # each, not N. Each pull is blended and synced before the next
        # one: the next get_batch_frames hands the previous pull's frames
        # back to the decoder.
        step = int(getattr(self, "_pull_k", 0) or 0) or len(frames)
        index = dict((fr, i) for i, fr in enumerate(frames))
        for s0 in range(0, len(frames), step):
            for sub in split_runs(self, frames[s0:s0 + step], snapshot, clips):
                pre = self._prefetch(sub, snapshot, clips)
                # the files (footage or proxy) this track decoded, for the log
                files = ", ".join(sorted(set(k[1] for k in self.decoders
                                             if k in track_keys)))
                vram_fatal_check("track %s decode, frames %d..%d, files %s" % (
                    track_id, sub[0], sub[-1], files or "none"))
                for fr in sub:
                    self._build_dst(fr, dark, bufs[index[fr]], snapshot, False, pre,
                                    sync=False, clips=clips, clear=False)
                _check(cudart.cudaStreamSynchronize(self.stream), "composite sync")
                vram_fatal_check("track %s blend, frames %d..%d, files %s" % (
                    track_id, sub[0], sub[-1], files or "none"))
                pre.clear()
                del pre
        # This track is blended (synced above). Without keep_dec its
        # decoders are closed now, so no source-size frame coexists with
        # the next layer's decode or the JPEG encode. Its stills are
        # freed either way.
        if not keep_dec:
            for key in list(self.decoders.keys()):
                if key in track_keys:
                    self._close_decoder(key, "track blended")
        for path in track_stills(self, frames, snapshot, clips):
            item = self.images.pop(path, None)
            if item is not None:
                cudart.cudaFree(item[0])
    # stills still uploaded that this batch does not use
    freed = 0
    for path in list(self.images.keys()):
        if path not in stills:
            item = self.images.pop(path)
            if item is not None:
                cudart.cudaFree(item[0])
            freed += 1


def track_stills(self, frames, snapshot, clips):
    # Still (image) paths and text texture keys (BUILD 309) of the given
    # clips in these frames
    out = set()
    for fr in frames:
        for item in self._layer_items(fr, snapshot):
            if item[0] in ("image", "text") and item[6][0] in clips:
                out.add(item[1])
    return out


# Debug trace of a refresh batch: text steps (e.g. free VRAM after each
# step) that sequence_preview_worker._log_refresh_vram prints. Nothing
# appends to it in normal runs; add .append() calls to trace a batch.
_refresh_trace = []

# Hard VRAM floor of the explicit refresh: free VRAM under this at any
# check point exits FLAVE (vram_fatal_check). Checked at each decoder
# open, after each layer pull's decode and blend, after each range's
# pool reserve and after each batch. _vram_base: free VRAM right after
# the latest pool reserve (set by fit_refresh_pool), so the exit message
# can show the drift since then.
REFRESH_VRAM_FATAL_MB = 1500
_vram_base = [None]


def vram_fatal_check(where):
    # Free VRAM under REFRESH_VRAM_FATAL_MB: print the numbers and exit the
    # process at once. Why exit instead of recovering: running the card
    # out of memory can freeze the whole PC, and a CUDA fault such as
    # error 700 is sticky in the shared CUDA context (nothing in-process
    # can recover it). os._exit skips Python cleanup that could touch the
    # GPU again. The plan's keep sits well above this floor, so reaching
    # it means an estimate was wrong.
    fv = free_vram()
    if fv is None or fv[0] >= (REFRESH_VRAM_FATAL_MB << 20):
        return
    base = _vram_base[0]
    print("ERROR: WORKER refresh VRAM critically low %s: free %d MB of %d MB "
          "(limit %d MB), drift since reserve %s. Exiting FLAVE now." % (
              where, fv[0] >> 20, fv[1] >> 20, REFRESH_VRAM_FATAL_MB,
              ("%+d MB" % ((fv[0] - base) >> 20)) if base is not None else "?"))
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(3)


def _free_mb():
    # free VRAM in MB, -1 when it cannot be read
    fv = free_vram()
    return (fv[0] >> 20) if fv else -1


def refresh_owned(gen):
    # Debug text: what FLAVE itself holds on the card for the refresh
    # (composite buffers, nvJPEG encoder states, stills, open decoders)
    imgs = [i for i in getattr(gen, "images", {}).values() if i is not None]
    bufs = getattr(gen, "gen_bufs", [])
    nvj = getattr(gen, "nvj", None)
    return ("buffers %d (%d MB), nvJPEG states %d, stills %d (%d MB), "
            "decoders open %d" % (
                len(bufs), (len(bufs) * gen.row * gen.seq_h) >> 20,
                len(nvj.states) if nvj is not None else 0, len(imgs),
                sum(i[3] * i[2] for i in imgs) >> 20,
                len(getattr(gen, "decoders", {}))))


def refresh_vram_guard(project_path, sequence_id, floor):
    # Free VRAM below floor bytes: close every decoder (their output
    # pools are the only refresh memory outside the reserved pool).
    # Returns (free before, free after) in MB, or None when nothing was
    # done.
    fv = free_vram()
    if fv is None or fv[0] >= floor:
        return None
    gen = _generator_for(project_path, sequence_id)
    if gen is None:
        return None
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        gen._close_all_decoders()
    finally:
        _GEN_LOCK.release()
    return fv[0] >> 20, _free_mb()


def kept_decoder_guard(project_path, sequence_id):
    # Worker, after each batch of a range whose decoders are kept open
    # (gen._keep_dec): measure the real free VRAM with every layer's
    # decoders held. Under the refresh keep, every decoder is closed and
    # _keep_dec is turned off, so the rest of the range closes each
    # layer's decoders after its blend (the plan's estimate was too low).
    # The next range's fit_refresh_pool sets _keep_dec again.
    gen = _generator_for(project_path, sequence_id)
    if gen is None or not getattr(gen, "_keep_dec", False):
        return
    fv = free_vram()
    if fv is None:
        return
    res = refresh_vram_guard(project_path, sequence_id, _refresh_keep(fv[1]))
    if res is not None:
        gen._keep_dec = False
        print("WORKER refresh decoders: free %d MB under keep %d MB with "
              "decoders kept open; closed (free %d MB), closing per layer "
              "for the rest of the range" % (
                  res[0], _refresh_keep(fv[1]) >> 20, res[1]))


def host_rgb(self, bufs):
    # BUILD 142 sink: each device buffer copied to host RGB24 bytes
    cudart = _libs()[0]
    size = self.row * self.seq_h
    out = []
    for b in bufs:
        host = ctypes.create_string_buffer(size)
        _check(cudart.cudaMemcpy(host, b, size, 2), "dst to host")
        out.append(host.raw)
    return out


def _jpeg_sink(quality):
    # BUILD 142 sink of the preview fill: ("jpeg", bytes) from nvJPEG, or
    # ("raw", host RGB) for the Qt fallback (no nvJPEG on this machine)
    def sink(self, bufs):
        lib = _nvjpeg_lib()
        if lib is not None:
            if self.nvj is None:
                self.nvj = _NvJpegEncoder(lib, self.stream)
            jpegs = encode_preview(self, bufs, quality)
            return [("jpeg", b) for b in jpegs]
        return [("raw", raw) for raw in host_rgb(self, bufs)]
    return sink

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
            pw, ph = _preview_dims(self)
            if (pw, ph) == (self.seq_w, self.seq_h):
                out = self.nvj.encode(src, self.seq_w, self.seq_h, self.row, quality)
            else:
                # Preview Size below 100%: scale on the card, encode that
                small = _scaled_buffers(self, 1, pw, ph)[0]
                resample_rgb(self, src, self.seq_w, self.seq_h, self.row,
                             small, pw, ph)
                out = self.nvj.encode(small, pw, ph, pw * 3, quality)
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
    # BUILD 145: worker=True always uses the frame engine (_generator_for),
    # player open or not; the GUI path (worker=False) still needs a player.
    if worker:
        gen = _generator_for(project_path, sequence_id)
        if gen is None:
            return None
        return gen.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)
    try:
        import sequence_timeline
        timeline = sequence_timeline._open_windows.get(int(sequence_id))
    except Exception:
        return None
    player = getattr(timeline, "_player", None)
    if player is None or player.closed:
        # GUI path: needs the player (BUILD 145: the worker returned above)
        return None
    if os.path.abspath(project_path) != os.path.abspath(timeline._project_db()):
        return None
    # BUILD 145: the frame engine is not released for the player any more
    # if worker:
    #     _release_headless()
    return player.frame_jpeg(int(frame_num), preview_jpeg_quality(), snapshot, worker)


def _release_headless():
    try:
        from sequence_preview_gpu import release_headless
        release_headless()
    except Exception as e:
        print(f"Preview headless GPU release failed: {e}")


def _generator_for(project_path, sequence_id):
    # Worker thread: the composer for this sequence's preview frames.
    # BUILD 145: the frame engine (one headless composer, its decoders stay
    # warm when a player opens or closes); the open player only when the
    # engine's GPU setup failed; else None.
    # Was (BUILD 141): the open player first (headless released), else the
    # headless composer.
    try:
        from sequence_preview_gpu import headless_for
        engine = headless_for(project_path, sequence_id)
    except Exception as e:
        print(f"Preview headless GPU failed: {e}")
        engine = None
    if engine is not None:
        return engine
    return _player_for(project_path, sequence_id)


def free_vram():
    # (free, total) dedicated device bytes from cudaMemGetInfo, or None
    try:
        cudart = _libs()[0]
        free = ctypes.c_size_t(0)
        total = ctypes.c_size_t(0)
        _check(cudart.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)),
               "mem info")
        return free.value, total.value
    except Exception:
        return None


# Device bytes per decoded frame per source pixel: the RGB output frame a
# SimpleDecoder keeps (3) plus its NV12 decode surface (1.5)
REFRESH_DECODE_BYTES_PX = 4.5
# Added on top of the keep's floor (see _refresh_keep)
REFRESH_CHUNK_CUSHION_MB = 256
_src_size = {}      # path -> (w, h) via ffprobe, or None


def _source_size(path):
    # Source width x height (ffprobe, cached per path), or None
    if path not in _src_size:
        try:
            from proxy_manager import probe_size
            _src_size[path] = probe_size(path)
        except Exception:
            _src_size[path] = None
    return _src_size[path]


# Share of total VRAM the refresh leaves free (never under
# REFRESH_VRAM_FATAL_MB), see _refresh_keep
REFRESH_VRAM_MARGIN = 0.25


def _refresh_keep(total_b):
    # Free VRAM (bytes) the refresh must always leave on a card of
    # total_b bytes: max(25% of total, 1500 MB) + 256 MB cushion
    return (max(int(REFRESH_VRAM_MARGIN * total_b), REFRESH_VRAM_FATAL_MB << 20)
            + (REFRESH_CHUNK_CUSHION_MB << 20))


def _free_pool(gen, n):
    # (lock held) free composite buffers beyond n. nvJPEG states are kept
    # up to max_states (the refresh reuses them); n None or 0 frees every
    # buffer and every state.
    cudart = _libs()[0]
    for b in gen.gen_bufs[n or 0:]:
        if b.value:
            cudart.cudaFree(b)
    del gen.gen_bufs[n or 0:]
    if gen.nvj is not None:
        gen.nvj.trim(gen.nvj.max_states if n else 0)


def refresh_plan(project_path, sequence_id, snapshot, total, pool_n):
    # Worker, once per explicit refresh pass, before any batch: the
    # per-range plan (see the memory model above _composite_by_layer).
    #
    # 1) Close every decoder and free the whole pool. BUILD 317: warm
    #    one buffer + one nvJPEG state and measure the state's cost;
    #    max_states = states that fit in NVJ_STATES_VRAM_SHARE of total
    #    VRAM (1..16, nvj_fit_states). Then reserve and
    #    warm a small pool (min(pool_n, max_states) buffers + nvJPEG
    #    states) and read free VRAM after it, so the real state cost is
    #    already out of free. budget = that free + the reserved buffers
    #    (they are part of every range's N) - the keep.
    # 2) Cut the sequence at every clip start and end on any track; merge
    #    neighbouring ranges whose layers at their first frame use the
    #    same decoder keys (file instances) and stills, so consecutive
    #    clips of one file are one range. Gaps are ranges too.
    # 3) Per range: still bytes (RGBA, 4 per pixel) and each video
    #    layer's decode cost per frame (REFRESH_DECODE_BYTES_PX x source
    #    w x h; sequence size when ffprobe fails); sum_per is their sum.
    #    want = min(pool_n, range length); room = budget - stills.
    #      - every frame + its full decode fits: N = k = want
    #      - else N gets the room left after pulls of max_states frames
    #        (capped at want) and k the room N leaves (max_states..N);
    #        if that N is under max_states, N = k = what fits with full
    #        pulls
    #      - no video layer: N = buffers that fit, k = 0
    #      - no room or no VRAM reading: N = 1
    # pool_n is the Preview Frame Cap: it only lowers N.
    # Returns [(start, end, n, per, still, k, held, keys)], end inclusive:
    # per = largest layer's decode bytes/frame when k is 0 (layers then
    # decode one at a time, N frames each), else 0 (decode is in held);
    # still = still bytes; k = pull size; held = k x sum_per; keys = the
    # range's decoder keys (frozenset). The worker passes these to
    # fit_refresh_pool at each range.
    gen = _generator_for(project_path, sequence_id)
    if gen is None or total <= 0:
        return []
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        gen._close_all_decoders()
        gen._refresh_hold = 0
        gen._keep_dec = False
        gen._pull_k = 0
        _free_pool(gen, None)
        # BUILD 317: the encoder and (below 100%) one Preview Size buffer
        # exist before the measure, so only buffer + state are in it
        pw, ph = _preview_dims(gen)
        lib = _nvjpeg_lib()
        if lib is not None and gen.nvj is None:
            gen.nvj = _NvJpegEncoder(lib, gen.stream)
        if lib is not None and (pw, ph) != (gen.seq_w, gen.seq_h):
            _scaled_buffers(gen, 1, pw, ph)
    finally:
        _GEN_LOCK.release()
    pool_px = int(gen.row) * int(gen.seq_h)
    fv0 = free_vram()
    if gen.nvj is not None:
        # warm 1 buffer + 1 state: the drop in free VRAM minus the buffer
        # is one state's cost at this encode size
        n1 = reserve_refresh_pool(project_path, sequence_id, 1)
        fv1 = free_vram()
        cost = None
        if n1 and fv0 and fv1:
            cost = fv0[0] - fv1[0] - pool_px
            if cost <= 0:
                cost = None     # allocator reuse: use the estimate
        _GEN_LOCK.acquire()
        try:
            gen._vram_total = fv0[1] if fv0 else 0
            m = nvj_fit_states(gen, pw, ph, cost)
            if cost is None:
                cost = gen._nvj_cost[1]
        finally:
            _GEN_LOCK.release()
        print("WORKER refresh states: max %d nvJPEG states at %dx%d (1 state "
              "%.0f MB %s, budget %d%% of %s MB VRAM)" % (
                  m, pw, ph, cost / float(1 << 20),
                  "measured" if gen._nvj_cost[3] else "estimated",
                  int(NVJ_STATES_VRAM_SHARE * 100 + 0.5),
                  (fv0[1] >> 20) if fv0 else "?"))
    n0 = max(1, min(int(pool_n), nvj_states_cap(gen)))
    n0 = reserve_refresh_pool(project_path, sequence_id, n0)
    fv = free_vram()
    keep = _refresh_keep(fv[1]) if fv else 0
    # the n0 reserved buffers count as free (the range's N includes them)
    budget = (fv[0] + n0 * pool_px - keep) if fv else 0
    if fv0 and fv and preview_log.RENDER_LOG:
        print("WORKER refresh states: %d nvJPEG states + %d buffers reserved, "
              "free %d -> %d MB (states %.0f MB measured)" % (
                  len(gen.nvj.states) if gen.nvj is not None else 0, n0,
                  fv0[0] >> 20, fv[0] >> 20,
                  (fv0[0] - fv[0] - n0 * pool_px) / float(1 << 20)))
    cuts = set([0, int(total)])
    for clips in snapshot[1].values():
        for clip in clips:
            for f in (int(clip[2]), int(clip[2]) + int(clip[3])):
                if 0 < f < total:
                    cuts.add(f)
    cuts = sorted(cuts)
    if preview_log.RENDER_LOG:
        print("WORKER refresh decoders: " + decoder_keys_text(gen, snapshot))
    ranges = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        items = gen._layer_items(a, snapshot)
        sig = tuple(sorted((it[0], it[16] if it[0] == "video" else it[1])
                           for it in items))
        if ranges and ranges[-1][2] == sig:
            ranges[-1][1] = b - 1
        else:
            ranges.append([a, b - 1, sig, items])
    plan = []
    pw, ph = _preview_dims(gen)
    if preview_log.RENDER_LOG:
        print("WORKER refresh plan: %d ranges, free %s of %s MB, keep %d MB, "
              "pool %.1f MB/frame, Preview Frame Cap %d, Preview Size %d%% "
              "(%dx%d)" % (
                  len(ranges), (fv[0] >> 20) if fv else "?",
                  (fv[1] >> 20) if fv else "?", keep >> 20,
                  pool_px / float(1 << 20), pool_n,
                  _preview_pct(gen), pw, ph))
    for a, b, _sig, items in ranges:
        per, still, big_path, big_size = 0, 0, None, None
        per_all = {}
        for it in items:
            if it[0] == "text":
                # BUILD 309: a text texture is sequence size (no probe)
                still += 4 * int(gen.seq_w) * int(gen.seq_h)
                continue
            size = _source_size(it[1]) or (int(gen.seq_w), int(gen.seq_h))
            if it[0] == "image":
                still += 4 * size[0] * size[1]
            elif it[0] == "video":
                cost = int(size[0] * size[1] * REFRESH_DECODE_BYTES_PX)
                per_all[it[16]] = cost
                if cost > per:
                    per, big_path, big_size = cost, it[1], size
        # Decoders stay open for the range and pull k frames at a time,
        # so the decode held is k x sum_per, not N x (see step 3 above).
        sum_per = sum(per_all.values())
        want = max(1, min(int(pool_n), b - a + 1))
        room = (budget - still) if fv is not None else 0
        k, held = 0, 0
        if fv is None or room <= 0:
            n = 1
        elif not sum_per:
            n = max(1, min(want, int(room // pool_px)))
        elif (pool_px + sum_per) * want <= room:
            n = k = want
        else:
            k0 = nvj_states_cap(gen)
            n = int((room - k0 * sum_per) // pool_px)
            if n < k0:
                n = k = max(1, int(room // (pool_px + sum_per)))
            else:
                n = min(want, n)
                k = max(k0, min(n, int((room - n * pool_px) // sum_per)))
        held = k * sum_per
        plan.append((a, b, n, 0 if k else per, still, k, held,
                     frozenset(per_all)))
        if preview_log.RENDER_LOG:
            print("WORKER refresh range %d..%d: N %d | largest source %s %s, "
                  "%d video layers %.1f MB/frame, pulls of %d frames (%d MB held), "
                  "stills %d MB" % (
                      a, b, n, ("%dx%d" % big_size) if big_size else "none",
                      big_path or "", len(per_all), sum_per / float(1 << 20),
                      k, held >> 20, still >> 20))
    return plan


def reserve_refresh_pool(project_path, sequence_id, n):
    # Worker, explicit refresh: hold a pool of n composite buffers and up
    # to max_states nvJPEG encoder states (min(n, max_states)); every card
    # pass reuses them, untrimmed, until release_refresh_pool. nvJPEG
    # allocates a state's device memory at its first encode, not at
    # create, so the n cleared buffers are encoded once here: that memory
    # is taken now and shows in the free VRAM read after the reserve,
    # instead of growing during the first batch.
    # Returns n, or 0 when the composer is missing or allocation failed
    # (the pool is then trimmed back to GEN_KEEP).
    gen = _generator_for(project_path, sequence_id)
    if gen is None or n <= 0:
        return 0
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        gen._refresh_hold = int(n)
        gen._gen_buffers(int(n))
        lib = _nvjpeg_lib()
        if lib is not None:
            if gen.nvj is None:
                gen.nvj = _NvJpegEncoder(lib, gen.stream)
            gen.nvj.reserve(int(n))
            # warm-up encode (see above)
            bufs = gen.gen_bufs[:int(n)]
            for b in bufs:
                _check(_libs()[0].cudaMemsetAsync(b, 0, gen.row * gen.seq_h,
                                                  gen.stream), "clear")
            # at the Preview Size: the states take that size's memory and
            # the small buffers are allocated before free VRAM is read
            encode_preview(gen, bufs, preview_jpeg_quality())
        return int(n)
    except Exception as e:
        print(f"Refresh pool reserve failed: {e}")
        gen._refresh_hold = 0
        try:
            _trim_gen(gen)
        except Exception:
            pass
        return 0
    finally:
        _GEN_LOCK.release()


def fit_refresh_pool(project_path, sequence_id, n, per, still, k=0, held=0,
                     keys=None):
    # Worker, at the first batch of each plan range: make the pool hold
    # exactly n frames and check it against the real free VRAM.
    # n, per, still, k, held, keys come from the range's refresh_plan
    # entry.
    # 1) Close the decoders whose key is not in keys (every decoder when
    #    keys is None). A kept decoder's memory is already out of the
    #    free VRAM read below and is counted again in held, so the fit
    #    errs low. _keep_dec = k > 0, _pull_k = k.
    # 2) If the pool is not n yet: predict from free VRAM before
    #    reserving (buffers are exactly row x height each, already held
    #    ones count as free). A shortfall comes out of the pulls first
    #    (_shrink_pull), then N is capped to what fits. Then free beyond
    #    n and reserve up to n.
    # 3) Read free VRAM after the reserve (also stored in _vram_base) and
    #    check that n x per + held + still still leaves the keep. If
    #    short: lower k first (decode is not allocated yet, so no
    #    re-reserve is needed); only if that cannot cover it, lower N by
    #    the shortfall and refit (up to 4 rounds). k never exceeds N.
    # 4) BUILD 317, N = 1 and still short: free nvJPEG states beyond what
    #    covers the shortfall (N = 1 encodes with one state) and re-read
    #    free VRAM. Still short and the free VRAM left after the decode
    #    and stills would be under REFRESH_VRAM_FATAL_MB +
    #    REFRESH_CHUNK_CUSHION_MB: return REFRESH_ABORT (the worker ends
    #    the refresh pass with a message); otherwise N = 1 runs as before
    #    (under the hard floor guard).
    # Returns the n held (0: reserve failed, REFRESH_ABORT: abort).
    gen = _generator_for(project_path, sequence_id)
    if gen is None:
        return 0
    k = int(k or 0)
    unit_dec = (int(held) // k) if k else 0     # decode bytes per pulled frame
    _GEN_LOCK.acquire()
    try:
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        if keys is None:
            gen._close_all_decoders()
        else:
            for key in list(gen.decoders.keys()):
                if key not in keys:
                    gen._close_decoder(key, "not in this range")
        gen._keep_dec = k > 0
        gen._pull_k = k
    finally:
        _GEN_LOCK.release()
    n = max(1, int(n))
    for _ in range(4):
        if k:
            k = min(k, n)
            held = k * unit_dec
            gen._pull_k = k
        if int(getattr(gen, "_refresh_hold", 0) or 0) != n:
            # predict before reserving, so the pool never grows into the
            # keep
            fv = free_vram()
            if fv is not None:
                nbufs = len(gen.gen_bufs)
                room = fv[0] + nbufs * int(gen.row) * int(gen.seq_h) - still \
                    - _refresh_keep(fv[1])
                # shortfall out of the pulls first (k, not under
                # max_states), so N still covers the range and leaves no
                # small leftover batch
                over = n * (per + int(gen.row) * int(gen.seq_h)) + held - room
                if over > 0 and k:
                    k, held = _shrink_pull(gen, k, unit_dec, over)
                fit = max(1, int((room - held) // (per + int(gen.row) * int(gen.seq_h))))
                n = min(n, fit)
                if k and k > n:
                    k = n
                    held = k * unit_dec
                    gen._pull_k = k
            _GEN_LOCK.acquire()
            try:
                _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
                gen._refresh_hold = 0
                _free_pool(gen, n)
            finally:
                _GEN_LOCK.release()
            if reserve_refresh_pool(project_path, sequence_id, n) != n:
                return 0
        fv = free_vram()
        if fv is None:
            return n
        _vram_base[0] = fv[0]
        short = _refresh_keep(fv[1]) - (fv[0] - n * per - held - still)
        if preview_log.RENDER_LOG:
            print("WORKER refresh pool: N %d reserved, free after %d of %d MB, "
                  "decode %d x %.1f MB + pulls of %d (%d MB) + stills %d MB, "
                  "keep %d MB%s" % (
                      n, fv[0] >> 20, fv[1] >> 20, n, per / float(1 << 20),
                      k, held >> 20, still >> 20, _refresh_keep(fv[1]) >> 20,
                      (", short %d MB" % (short >> 20)) if short > 0 else ""))
        if short <= 0:
            return n
        if n <= 1:
            return _fit_last_resort(gen, n, per, held, still, short)
        if k:
            # pulls shrink first (no re-reserve: decode is not allocated
            # yet); N only for what that cannot cover
            k2, held2 = _shrink_pull(gen, k, unit_dec, short)
            short -= held - held2
            k, held = k2, held2
            if short <= 0:
                if preview_log.RENDER_LOG:
                    print("WORKER refresh pool: pulls lowered to %d (%d MB), "
                          "N %d kept" % (k, held >> 20, n))
                return n
        unit = per + int(gen.row) * int(gen.seq_h)
        n = max(1, n - (short + unit - 1) // unit)
        if k and k > n:
            # pulls never above N (frees decode with it)
            k = n
            held = k * unit_dec
            gen._pull_k = k
    return n


# fit_refresh_pool result: not enough VRAM even at N = 1 with the nvJPEG
# states trimmed; the worker aborts the refresh pass (BUILD 317)
REFRESH_ABORT = -1
REFRESH_ABORT_TEXT = "Memory Low! Reduce Preview Size"


def _fit_last_resort(gen, n, per, held, still, short):
    # fit_refresh_pool step 4 (N = 1, short bytes under the keep). Returns
    # n or REFRESH_ABORT.
    nvj = gen.nvj
    fv = free_vram()
    if nvj is not None and len(nvj.states) > 1 and fv is not None:
        known = getattr(gen, "_nvj_cost", None)
        cost = known[1] if known else int(NVJ_STATE_BYTES_PX * gen.seq_w * gen.seq_h)
        had = len(nvj.states)
        keep_st = max(1, had - int((short + cost - 1) // max(1, cost)))
        _GEN_LOCK.acquire()
        try:
            _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
            nvj.trim(keep_st)
        finally:
            _GEN_LOCK.release()
        fv = free_vram()
        if fv is None:
            return n
        _vram_base[0] = fv[0]
        short = _refresh_keep(fv[1]) - (fv[0] - n * per - held - still)
        print("WORKER refresh pool: N 1 short of the keep, nvJPEG states %d -> "
              "%d, free now %d MB%s" % (
                  had, len(nvj.states), fv[0] >> 20,
                  (", short %d MB" % (short >> 20)) if short > 0 else ""))
    if fv is None or short <= 0:
        return n
    left = fv[0] - n * per - held - still
    if left >= ((REFRESH_VRAM_FATAL_MB + REFRESH_CHUNK_CUSHION_MB) << 20):
        return n
    print("WORKER refresh aborted: free %d MB of %d MB, %d MB left after the "
          "decode and stills of N 1 (needs %d MB). %s" % (
              fv[0] >> 20, fv[1] >> 20, left >> 20,
              REFRESH_VRAM_FATAL_MB + REFRESH_CHUNK_CUSHION_MB,
              REFRESH_ABORT_TEXT))
    return REFRESH_ABORT


def _shrink_pull(gen, k, unit_dec, over):
    # Lower the pull size k by enough frames to free over bytes, but not
    # under min(k, max_states). Sets gen._pull_k; returns (k, held bytes).
    floor = min(k, nvj_states_cap(gen))
    k = max(floor, k - int((over + unit_dec - 1) // unit_dec)) if unit_dec else k
    gen._pull_k = k
    return k, k * unit_dec


def refresh_pool_size(project_path, sequence_id):
    # Frames the composer's refresh pool holds (0: none, e.g. the
    # composer was replaced or released since the reserve)
    gen = _generator_for(project_path, sequence_id)
    return int(getattr(gen, "_refresh_hold", 0) or 0) if gen is not None else 0


def release_refresh_pool(project_path, sequence_id):
    # End of the explicit refresh: clear the refresh flags, trim the pool
    # back to GEN_KEEP and close every decoder. Decoders grown to big
    # output pools by the refresh pulls only give that memory back when
    # closed; they reopen on demand at the normal 1-frame size.
    gen = _generator_for(project_path, sequence_id)
    if gen is None:
        return
    _GEN_LOCK.acquire()
    try:
        gen._refresh_hold = 0
        gen._keep_dec = False
        gen._pull_k = 0
        _check(_libs()[1].cuCtxSetCurrent(gen.cuda_ctx), "ctx")
        _trim_gen(gen)
        gen._close_all_decoders()
    except Exception as e:
        print(f"Refresh pool release failed: {e}")
    finally:
        _GEN_LOCK.release()


def preview_jpeg_quality():
    # Global previewJPEG_quality, 1..100 (clamped); 90 if missing or bad.
    # Read per frame so a change applies to the next encode.
    try:
        q = int(str(_get_global_setting("previewJPEG_quality", "90")).strip())
    except Exception:
        return 90
    return max(1, min(100, q))


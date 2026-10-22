# captions_generate.py
# Generate Captions (BUILD 225): transcribe one audio clip with faster-whisper
# (local) and group the words into short caption clips. No Qt here; the
# Sequence Timeline wiring is generate_captions in
# sequence_timeline_captions.py.
#
# Audio: the clip's source range (in_frame .. in_frame + length_frames, in
# sequence frames, the same mapping as the preview audio mix) read from the
# project's mono 48 kHz AudioTmp cache and decimated to 16 kHz. If the
# source is not in the cache, FFmpeg extracts the range to a 16 kHz mono
# temp wav ({ProjectName}_{sequence_id}_CaptionAudio.wav, removed after).
#
# Grouping: a new caption starts after a pause of PAUSE_SPLIT_S or more,
# after a word ending a sentence, or when the next word would pass
# MAX_WORDS words or MAX_SECONDS. ASCII only.
#
# Console log (BUILD 275): lines prefixed "[captions]" report the library
# versions, CUDA device count, the device / compute type tried, the full
# error and traceback when CUDA fails, model load time, audio source and
# load time, progress at most every PROGRESS_LOG_S seconds and a summary.

import os
import subprocess
import sys
import time
import traceback

WHISPER_MODEL = "small"           # multilingual; ~460 MB download on first use
WHISPER_BEAM_SIZE = 5
PAUSE_SPLIT_S = 0.30              # gap between words that starts a new caption
MAX_WORDS = 7                     # words per caption
MAX_SECONDS = 3.0                 # caption duration (first word start .. last end)
SENTENCE_END = (".", "?", "!")    # a word ending with one of these ends a caption
SAMPLE_RATE = 16000               # faster-whisper input rate
PROGRESS_LOG_S = 30.0             # console progress line interval (seconds)

# Whisper 2-letter language -> the caption track language (ISO 639-2)
LANGUAGE_639_2 = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu",
                  "it": "ita", "pt": "por", "ja": "jpn", "ko": "kor",
                  "zh": "zho", "ru": "rus", "ar": "ara", "hi": "hin"}

INSTALL_HINT = "pip install faster-whisper"

# BUILD 231: approximate download size per model, for the download prompt
MODEL_SIZES = {"tiny": "75 MB", "base": "145 MB", "small": "460 MB",
               "medium": "1.5 GB", "large-v3": "3 GB", "large-v3-turbo": "1.6 GB"}


CUDA_HINT = ("hint: on Windows this is most often missing cuBLAS / cuDNN DLLs "
             "for CTranslate2 (pip install nvidia-cublas-cu12 nvidia-cudnn-cu12, "
             "or put the CUDA 12 cuBLAS and cuDNN 9 DLLs on PATH)")


def _log(msg):
    # One console line with the [captions] prefix; never raises (no console
    # under pythonw)
    try:
        print("[captions] " + msg, flush=True)
    except Exception:
        pass


def _log_exception(what, exc):
    # Full exception type, message and traceback, one console line each
    _log("%s: %s: %s" % (what, type(exc).__name__, exc))
    try:
        for chunk in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for line in chunk.rstrip().splitlines():
                _log("  " + line)
    except Exception:
        pass


def cpu_threads():
    # Threads for CPU transcription: all logical cores (4 when unknown)
    return os.cpu_count() or 4


def device_text(device):
    # Progress label / console text of a device: "CUDA float16" or
    # "CPU int8, N threads"
    if device == "cuda":
        return "CUDA float16"
    return "CPU int8, %d threads" % cpu_threads()


def log_environment():
    # faster-whisper / ctranslate2 versions and the CUDA device count
    # CTranslate2 sees (0 means it will not run on the GPU)
    try:
        import faster_whisper
        fw = getattr(faster_whisper, "__version__", "unknown")
    except Exception as e:
        fw = "not importable (%s)" % e
    try:
        import ctranslate2
        ct = getattr(ctranslate2, "__version__", "unknown")
        try:
            count = str(ctranslate2.get_cuda_device_count())
        except Exception as e:
            count = "unavailable (%s: %s)" % (type(e).__name__, e)
    except Exception as e:
        ct, count = "not importable (%s)" % e, "unavailable"
    _log("faster-whisper %s, ctranslate2 %s, CUDA devices: %s, CPU cores: %s"
         % (fw, ct, count, os.cpu_count()))


class CaptionGenError(Exception):
    pass


class MissingWhisper(CaptionGenError):
    pass


def whisper_available():
    # True when faster_whisper can be imported (checked without importing it)
    try:
        import importlib.util
        return importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        return False


def group_words(words, pause_s=PAUSE_SPLIT_S, max_words=MAX_WORDS,
                max_seconds=MAX_SECONDS):
    # words: [(start_s, end_s, text)] in time order. Returns
    # [(start_s, end_s, text)] captions; text = the words joined by spaces.
    out = []
    cur = []
    for w in words or []:
        start, end, text = float(w[0]), float(w[1]), (w[2] or "").strip()
        if not text:
            continue
        if end < start:
            end = start
        if cur:
            gap = start - cur[-1][1]
            if (gap >= pause_s or len(cur) >= max_words
                    or end - cur[0][0] > max_seconds
                    or cur[-1][2].endswith(SENTENCE_END)):
                out.append(cur)
                cur = []
        cur.append((start, end, text))
    if cur:
        out.append(cur)
    return [(g[0][0], g[-1][1], " ".join(t for _s, _e, t in g)) for g in out]


def groups_to_frames(groups, fps, clip_start, clip_length):
    # Caption times (seconds from the clip's source in) -> sequence frames:
    # frame = clip_start + t * fps, clamped to the clip's range, each caption
    # at least 1 frame and never overlapping the one before. Returns
    # [(start_frame, length_frames, text)].
    fps = float(fps or 30.0)
    lo = int(clip_start)
    hi = lo + max(0, int(clip_length))
    out = []
    prev_end = lo
    for start_s, end_s, text in groups or []:
        a = lo + int(round(float(start_s) * fps))
        b = lo + int(round(float(end_s) * fps))
        a = max(a, prev_end, lo)
        b = min(max(b, a + 1), hi)
        if a >= hi or b <= a:
            continue
        out.append((a, b - a, text))
        prev_end = b
    return out


def _frame_to_sample(frame, fps, rate):
    return int(round(float(frame) * rate / float(fps or 30.0)))


def pcm_from_audiotmp(project_db, basename, in_frame, length_frames, fps):
    # float32 16 kHz mono for the source range from the AudioTmp cache, or
    # None when the source is not cached. 48 kHz -> 16 kHz: mean of each 3.
    import numpy as np
    from peaks import load_audiotmp, PEAKS_SAMPLE_RATE
    pcm = load_audiotmp(project_db, basename)
    if not pcm:
        return None
    src = np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 2)
    rate = int(PEAKS_SAMPLE_RATE)
    a = _frame_to_sample(in_frame, fps, rate)
    b = a + _frame_to_sample(length_frames, fps, rate)
    seg = src[max(0, a):max(0, min(len(src), b))].astype(np.float32) / 32768.0
    if rate == 48000:
        n = len(seg) // 3 * 3
        seg = seg[:n].reshape(-1, 3).mean(axis=1)
    elif rate != SAMPLE_RATE:
        idx = (np.arange(int(len(seg) * SAMPLE_RATE / rate)) * rate / SAMPLE_RATE)
        seg = seg[idx.astype(np.int64)]
    return seg.astype(np.float32)


def pcm_from_ffmpeg(source_path, start_s, dur_s, wav_path, cancel=None):
    # FFmpeg: source range -> 16 kHz mono s16 wav at wav_path, read back as
    # float32 and the wav removed. None on failure.
    import numpy as np
    import wave
    if not source_path or not os.path.isfile(source_path):
        return None
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-ss", "%.6f" % max(0.0, start_s), "-t", "%.6f" % max(0.0, dur_s),
           "-i", source_path, "-map", "0:a:0", "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", wav_path]
    flags = 0x08000000 if sys.platform.startswith("win") else 0
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, creationflags=flags)
        if r.returncode != 0 or not os.path.isfile(wav_path):
            return None
        with wave.open(wav_path, "rb") as w:
            data = w.readframes(w.getnframes())
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    except Exception:
        return None
    finally:
        try:
            if os.path.isfile(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


_models = {}


def model_size_text(name=WHISPER_MODEL):
    return MODEL_SIZES.get(name, "unknown size")


def model_cache_dir():
    # The Hugging Face hub cache the model is read from / downloaded to
    # (HF_HOME\hub; project_manager.py sets HF_HOME to models next to the
    # code files)
    try:
        from huggingface_hub import constants
        return constants.HF_HUB_CACHE
    except Exception:
        home = os.environ.get("HF_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache", "huggingface")
        return os.environ.get("HF_HUB_CACHE") or os.path.join(home, "hub")


def model_present(name=WHISPER_MODEL):
    # True when the model's files are already in the local cache. Local
    # lookup only (local_files_only=True): never downloads, no network.
    try:
        from faster_whisper.utils import download_model
        path = download_model(name, local_files_only=True)
    except Exception:
        return False
    # model.bin, config.json and tokenizer.json all on disk (a cancelled
    # download can leave a snapshot with only some files; without
    # tokenizer.json faster-whisper would fetch one from the hub)
    return bool(path) and all(os.path.isfile(os.path.join(path, f)) for f in
                              ("model.bin", "config.json", "tokenizer.json"))


# BUILD 232: the files faster-whisper's download_model fetches
MODEL_PATTERNS = ["config.json", "preprocessor_config.json", "model.bin",
                  "tokenizer.json", "vocabulary.*"]


def model_repo_id(name=WHISPER_MODEL):
    # Hugging Face repo of a faster-whisper model name (its own table)
    if "/" in name:
        return name
    from faster_whisper.utils import _MODELS
    return _MODELS[name]


def _hook_class(state, cancel):
    # tqdm subclass for hf_hub_download(tqdm_class=): never prints; counts
    # the current file's bytes into state["cur"] (n starts at the resumed
    # size) and aborts the download (CaptionGenError) once cancel is set
    from tqdm.std import tqdm

    class _Hook(tqdm):
        def __init__(self, *a, **k):
            k["disable"] = True
            super().__init__(*a, **k)
            self.n = int(k.get("initial") or 0)
            state["cur"] = self.n

        def update(self, n=1):
            if cancel is not None and cancel.is_set():
                raise CaptionGenError("cancelled")
            self.n += int(n or 0)
            state["cur"] = self.n

    return _Hook


def incomplete_bytes(name=WHISPER_MODEL):
    # Largest *.incomplete blob of the model's repo in the hub cache (the
    # file being downloaded), 0 if none
    try:
        repo = model_repo_id(name)
        blobs = os.path.join(model_cache_dir(),
                             "models--" + repo.replace("/", "--"), "blobs")
        sizes = [os.path.getsize(os.path.join(blobs, f)) for f in os.listdir(blobs)
                 if f.endswith(".incomplete")]
        return max(sizes) if sizes else 0
    except Exception:
        return 0


def download_model_files(name=WHISPER_MODEL, state=None, cancel=None):
    # Worker thread, after the user's Yes. Lists the repo's files
    # (MODEL_PATTERNS, with sizes) and fetches each with hf_hub_download
    # into the hub cache. state (dict,
    # read by the GUI): total, done (bytes of finished files), cur (bytes of
    # the current file), hook (True when hf_hub_download takes tqdm_class:
    # cur is live and Cancel aborts the transfer; otherwise the GUI reads
    # incomplete_bytes and a cancelled file finishes in the background).
    # Raises CaptionGenError.
    import fnmatch
    import inspect
    state = {} if state is None else state
    try:
        from huggingface_hub import HfApi, hf_hub_download
        repo = model_repo_id(name)
        info = HfApi().model_info(repo, files_metadata=True)
        files = [(s.rfilename, int(s.size or 0)) for s in info.siblings
                 if any(fnmatch.fnmatch(s.rfilename, p) for p in MODEL_PATTERNS)]
    except CaptionGenError:
        raise
    except Exception as e:
        raise CaptionGenError("Could not download the Whisper model:\n%s" % e)
    hook = "tqdm_class" in inspect.signature(hf_hub_download).parameters
    state.update({"total": sum(sz for _f, sz in files), "done": 0, "cur": 0,
                  "hook": hook})
    # Plain HTTP transfer, not hf_xet, for these calls: it stops on an
    # exception from the progress hook (Cancel); hf_xet ignores those and
    # keeps downloading. huggingface_hub 1.x+ deletes the partial
    # *.incomplete file when a transfer stops (older versions resume it).
    try:
        from huggingface_hub import constants as hf_constants
        old_xet = getattr(hf_constants, "HF_HUB_DISABLE_XET", None)
        hf_constants.HF_HUB_DISABLE_XET = True
    except Exception:
        hf_constants, old_xet = None, None
    try:
        _download_files(repo, files, state, cancel, hook, hf_hub_download)
    finally:
        if hf_constants is not None:
            if old_xet is None:
                try:
                    del hf_constants.HF_HUB_DISABLE_XET
                except AttributeError:
                    pass
            else:
                hf_constants.HF_HUB_DISABLE_XET = old_xet
    if not model_present(name):
        raise CaptionGenError("The Whisper model download is incomplete.")


def _download_files(repo, files, state, cancel, hook, hf_hub_download):
    for fname, size in files:
        if cancel is not None and cancel.is_set():
            raise CaptionGenError("cancelled")
        state["cur"] = 0
        kw = {"tqdm_class": _hook_class(state, cancel)} if hook else {}
        try:
            hf_hub_download(repo, fname, **kw)
        except CaptionGenError:
            raise
        except Exception as e:
            if cancel is not None and cancel.is_set():
                raise CaptionGenError("cancelled")
            raise CaptionGenError("Could not download the Whisper model:\n%s" % e)
        state["done"] += size
        state["cur"] = 0


def _load_model(device, allow_download=False):
    # One WhisperModel per device, kept for the session. cuda: float16;
    # cpu: int8 on cpu_threads() threads. allow_download False: local files
    # only (BUILD 231: a download needs the user's Yes in generate_captions
    # first).
    from faster_whisper import WhisperModel
    m = _models.get(device)
    if m is None:
        ctype = "float16" if device == "cuda" else "int8"
        kw = {"cpu_threads": cpu_threads()} if device == "cpu" else {}
        _log('loading model "%s" on %s' % (WHISPER_MODEL, device_text(device)))
        t0 = time.monotonic()
        m = WhisperModel(WHISPER_MODEL, device=device, compute_type=ctype,
                         local_files_only=not allow_download, **kw)
        _log("model loaded on %s in %.1f s" % (device, time.monotonic() - t0))
        _models[device] = m
    else:
        _log('model "%s" on %s already loaded' % (WHISPER_MODEL, device_text(device)))
    return m


def _run_whisper(model, audio, cancel, progress=None, device="", state=None):
    # progress(seconds_done, seconds_total) after each segment. state
    # (dict): pos (audio seconds reached) and segments, kept up to date so
    # a failure can report how far it got. Console progress at most every
    # PROGRESS_LOG_S seconds.
    state = {} if state is None else state
    state["pos"], state["segments"] = 0.0, 0
    t0 = time.monotonic()
    segments, info = model.transcribe(audio, beam_size=WHISPER_BEAM_SIZE,
                                      word_timestamps=True, vad_filter=True)
    total = len(audio) / float(SAMPLE_RATE)
    after_vad = getattr(info, "duration_after_vad", None)
    _log("transcribing on %s: %s of audio%s, language %s" % (
        device_text(device), _hms(total),
        (" (%s of speech after VAD)" % _hms(after_vad)) if after_vad is not None else "",
        getattr(info, "language", "") or "?"))
    words = []
    last_log = t0
    for seg in segments:      # lazy: decoding happens while iterating
        if cancel is not None and cancel.is_set():
            raise CaptionGenError("cancelled")
        for w in (seg.words or []):
            words.append((float(w.start), float(w.end), w.word))
        pos = min(total, float(seg.end))
        state["pos"] = pos
        state["segments"] += 1
        if progress is not None:
            progress(pos, total)
        now = time.monotonic()
        if now - last_log >= PROGRESS_LOG_S:
            last_log = now
            el = now - t0
            _log("progress %s: %s of %s audio, %s elapsed, %.1fx realtime, "
                 "%d segments" % (device, _hms(pos), _hms(total), _hms(el),
                                  pos / el if el > 0 else 0.0, state["segments"]))
    if cancel is not None and cancel.is_set():
        raise CaptionGenError("cancelled")
    state["seconds"] = time.monotonic() - t0
    return words, getattr(info, "language", "") or ""


def _hms(seconds):
    s = max(0, int(seconds or 0))
    return "%d:%02d:%02d" % (s // 3600, s // 60 % 60, s % 60)


def transcribe(audio, cancel=None, allow_download=False, progress=None, stage=None,
               stats=None):
    # (words [(start_s, end_s, text)], language 2-letter, device used).
    # CUDA first (float16); any CUDA / cuDNN failure falls back to CPU int8
    # (logged with its traceback). Without allow_download a model missing
    # from the cache is an error (nothing is downloaded). Before each step:
    # stage("device:<text>") (the device text for the progress label; on
    # the CPU fallback it ends with "; CUDA failed", or "; restarted after
    # CUDA error" when CUDA failed after segments were already decoded), stage("loading"), stage("transcribing");
    # progress as in _run_whisper. stats (dict) gets segments and
    # transcribe_s.
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise MissingWhisper(INSTALL_HINT)
    if not allow_download and not model_present():
        raise CaptionGenError('The Whisper "%s" model is not downloaded.' % WHISPER_MODEL)
    stage = stage or (lambda name: None)
    stats = {} if stats is None else stats
    log_environment()
    state = {}
    step = "model load"
    try:
        stage("device:" + device_text("cuda"))
        stage("loading")
        m = _load_model("cuda", allow_download)
        step = "transcription"
        stage("transcribing")
        words, lang = _run_whisper(m, audio, cancel, progress, "cuda", state)
        stats.update(segments=state.get("segments", 0),
                     transcribe_s=state.get("seconds", 0.0))
        return words, lang, "cuda"
    except CaptionGenError:
        raise
    except Exception as e:
        _models.pop("cuda", None)
        _log_exception("CUDA %s failed" % step, e)
        _log(CUDA_HINT)
    restarted = step == "transcription" and (state.get("segments", 0) > 0
                                             or state.get("pos", 0.0) > 0)
    if step == "transcription":
        _log("!!! CUDA FAILED DURING TRANSCRIPTION at %s (%d segments); "
             "RESTARTING FROM THE BEGINNING ON %s !!!" % (
                 _hms(state.get("pos", 0.0)), state.get("segments", 0),
                 device_text("cpu")))
    else:
        _log("falling back to %s" % device_text("cpu"))
    stage("device:" + device_text("cpu")
          + ("; restarted after CUDA error" if restarted else "; CUDA failed"))
    stage("loading")
    m = _load_model("cpu", allow_download)
    stage("transcribing")
    words, lang = _run_whisper(m, audio, cancel, progress, "cpu", state)
    stats.update(segments=state.get("segments", 0),
                 transcribe_s=state.get("seconds", 0.0), restarted=restarted)
    return words, lang, "cpu"


def clip_audio(info, cancel=None):
    # info: project_db, sequence_id, basename, source_path, in_frame,
    # length_frames, fps. float32 16 kHz mono or raises CaptionGenError.
    t0 = time.monotonic()
    audio = pcm_from_audiotmp(info["project_db"], info["basename"],
                              info["in_frame"], info["length_frames"], info["fps"])
    source = "AudioTmp cache (48 kHz -> 16 kHz)"
    if audio is None:
        source = "ffmpeg extract (16 kHz)"
        _log("%s not in the AudioTmp cache; extracting with ffmpeg"
             % (info["basename"] or "source"))
        from sequence_preview import temp_file_path
        wav = temp_file_path(info["project_db"], "CaptionAudio", "wav",
                             info["sequence_id"])
        fps = float(info["fps"] or 30.0)
        audio = pcm_from_ffmpeg(info["source_path"], info["in_frame"] / fps,
                                info["length_frames"] / fps, wav, cancel)
    if audio is None or len(audio) == 0:
        _log("no audio from %s after %.1f s" % (source, time.monotonic() - t0))
        raise CaptionGenError("No audio found for this clip's source.")
    _log("audio: %s, %d samples (%s) loaded in %.1f s" % (
        source, len(audio), _hms(len(audio) / float(SAMPLE_RATE)),
        time.monotonic() - t0))
    return audio


def generate(info, cancel=None, transcribe_fn=None, progress=None, stage=None):
    # Worker-thread job: audio + transcription + grouping. Returns
    # {"groups": [(start_s, end_s, text)], "language": "eng", "device": ...}
    # (times from the clip's source in). info["allow_download"]: the user
    # said Yes to the model download. transcribe_fn(audio, cancel, progress)
    # replaces transcribe (tests). progress / stage: see transcribe.
    t0 = time.monotonic()
    _log("generate: %s, in %d, %d frames at %.3f fps" % (
        info.get("basename") or "clip", int(info["in_frame"]),
        int(info["length_frames"]), float(info["fps"] or 30.0)))
    audio = clip_audio(info, cancel)
    stats = {}
    if transcribe_fn is not None:
        words, lang, device = transcribe_fn(audio, cancel, progress)
    else:
        words, lang, device = transcribe(audio, cancel,
                                         bool(info.get("allow_download")),
                                         progress, stage, stats)
    if cancel is not None and cancel.is_set():
        _log("cancelled after %s" % _hms(time.monotonic() - t0))
        raise CaptionGenError("cancelled")
    wall = time.monotonic() - t0
    dur = len(audio) / float(SAMPLE_RATE)
    _log("done: %s wall (%s transcribing), %s audio, %.1fx realtime, "
         "%d segments, %d words, device %s%s" % (
             _hms(wall), _hms(stats.get("transcribe_s", 0.0)), _hms(dur),
             dur / wall if wall > 0 else 0.0, stats.get("segments", 0),
             len(words), device_text(device) if device in ("cuda", "cpu") else device,
             " (restarted after CUDA error)" if stats.get("restarted") else ""))
    return {"groups": group_words(words),
            "language": LANGUAGE_639_2.get((lang or "").lower(), lang or "eng"),
            "device": device, "words": len(words)}

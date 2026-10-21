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

import os
import subprocess
import sys

WHISPER_MODEL = "small"           # multilingual; ~460 MB download on first use
WHISPER_BEAM_SIZE = 5
PAUSE_SPLIT_S = 0.30              # gap between words that starts a new caption
MAX_WORDS = 7                     # words per caption
MAX_SECONDS = 3.0                 # caption duration (first word start .. last end)
SENTENCE_END = (".", "?", "!")    # a word ending with one of these ends a caption
SAMPLE_RATE = 16000               # faster-whisper input rate

# Whisper 2-letter language -> the caption track language (ISO 639-2)
LANGUAGE_639_2 = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu",
                  "it": "ita", "pt": "por", "ja": "jpn", "ko": "kor",
                  "zh": "zho", "ru": "rus", "ar": "ara", "hi": "hin"}

INSTALL_HINT = "pip install faster-whisper"


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


def _load_model(device):
    # One WhisperModel per device, kept for the session. cuda: float16;
    # cpu: int8.
    from faster_whisper import WhisperModel
    m = _models.get(device)
    if m is None:
        ctype = "float16" if device == "cuda" else "int8"
        m = WhisperModel(WHISPER_MODEL, device=device, compute_type=ctype)
        _models[device] = m
    return m


def _run_whisper(model, audio, cancel):
    segments, info = model.transcribe(audio, beam_size=WHISPER_BEAM_SIZE,
                                      word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:      # lazy: decoding happens while iterating
        if cancel is not None and cancel.is_set():
            raise CaptionGenError("cancelled")
        for w in (seg.words or []):
            words.append((float(w.start), float(w.end), w.word))
    return words, getattr(info, "language", "") or ""


def transcribe(audio, cancel=None):
    # (words [(start_s, end_s, text)], language 2-letter, device used).
    # CUDA first (float16); any CUDA / cuDNN failure falls back to CPU int8.
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise MissingWhisper(INSTALL_HINT)
    try:
        words, lang = _run_whisper(_load_model("cuda"), audio, cancel)
        return words, lang, "cuda"
    except CaptionGenError:
        raise
    except Exception:
        _models.pop("cuda", None)
    words, lang = _run_whisper(_load_model("cpu"), audio, cancel)
    return words, lang, "cpu"


def clip_audio(info, cancel=None):
    # info: project_db, sequence_id, basename, source_path, in_frame,
    # length_frames, fps. float32 16 kHz mono or raises CaptionGenError.
    audio = pcm_from_audiotmp(info["project_db"], info["basename"],
                              info["in_frame"], info["length_frames"], info["fps"])
    if audio is None:
        from sequence_preview import temp_file_path
        wav = temp_file_path(info["project_db"], "CaptionAudio", "wav",
                             info["sequence_id"])
        fps = float(info["fps"] or 30.0)
        audio = pcm_from_ffmpeg(info["source_path"], info["in_frame"] / fps,
                                info["length_frames"] / fps, wav, cancel)
    if audio is None or len(audio) == 0:
        raise CaptionGenError("No audio found for this clip's source.")
    return audio


def generate(info, cancel=None, transcribe_fn=None):
    # Worker-thread job: audio + transcription + grouping. Returns
    # {"groups": [(start_s, end_s, text)], "language": "eng", "device": ...}
    # (times from the clip's source in). transcribe_fn replaces transcribe
    # (tests).
    audio = clip_audio(info, cancel)
    words, lang, device = (transcribe_fn or transcribe)(audio, cancel)
    return {"groups": group_words(words),
            "language": LANGUAGE_639_2.get((lang or "").lower(), lang or "eng"),
            "device": device, "words": len(words)}

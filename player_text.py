# player_text.py
# WYSIWYG Text clips (text_clip.py) as compositor layers. A
# text clip is a layer of kind "text" (sequence_player._layer_items): its
# picture is text_clip.render_text_image, a sequence-size transparent
# frame, converted to straight-alpha RGBA8888 and uploaded to device
# memory like a still (_image), then blended by blend_layer with bpp 4
# (its alpha is the text coverage). The layer path is a synthetic key
# "text:<clip id>:<style hash>:<w>x<h>" (text_layer_path), so the
# texture cache (gen.images, shared with stills and freed by the same
# release / refresh code) holds one texture per (clip, style, sequence
# size). text_texture frees the clip's texture of an older style on a new
# upload and keeps at most TEXT_TEX_MAX text textures per compositor
# (least recently used freed first; one is seq_w x seq_h x 4 bytes of
# VRAM, 8 MB at 1080p). ASCII only.

import ctypes
import hashlib

TEXT_PATH_PREFIX = "text:"
TEXT_TEX_MAX = 8


def style_hash(style):
    # Short hash of a style tuple (text_clip.TEXT_STYLE_KEYS order: text,
    # font_family, font_size, color, x, y and the shadow fields)
    return hashlib.md5(repr(tuple(style)).encode("utf-8", "replace")).hexdigest()[:16]


def text_layer_path(clip_id, style, seq_w, seq_h):
    # The layer path / texture key of a text clip with this style
    return "%s%d:%s:%dx%d" % (TEXT_PATH_PREFIX, int(clip_id), style_hash(style),
                              int(seq_w), int(seq_h))


def is_text_path(path):
    return isinstance(path, str) and path.startswith(TEXT_PATH_PREFIX)


def _clip_of(path):
    # clip id part of a text layer path
    return path[len(TEXT_PATH_PREFIX):].split(":", 1)[0]


def text_rgba(clip_id, style, seq_w, seq_h):
    # Sequence-size QImage, Format_RGBA8888 with straight (not
    # premultiplied) alpha, as blend_layer reads a bpp 4 source
    from PySide6.QtGui import QImage
    from text_clip import render_text_image
    style = tuple(style)
    text, family, size, color, x, y = style[:6]
    img = render_text_image(text, family, size, color, x, y,
                            int(seq_w), int(seq_h), clip_key=clip_id,
                            shadow=style[6:11] or None)
    return img.convertToFormat(QImage.Format_RGBA8888)


def text_texture(gen, path, clip_id, style):
    # (ptr, w, h, stride) of the clip's uploaded text image (gen.images
    # [path]), uploading it on a miss; None if the style is unknown.
    # Caller holds the compositor's lock and CUDA context (_build_dst).
    images = gen.images
    if path in images:
        item = images.pop(path)
        images[path] = item          # most recently used last
        return item
    if style is None:
        return None
    from sequence_player_gpu import _libs, _check
    cudart = _libs()[0]
    # Free this clip's older style and the least recently used text
    # textures beyond TEXT_TEX_MAX - 1 (after the stream's pending blends)
    mine = _clip_of(path)
    texts = [k for k in images if is_text_path(k)]
    drop = [k for k in texts if _clip_of(k) == mine]
    rest = [k for k in texts if k not in drop]
    drop += rest[:max(0, len(rest) - (TEXT_TEX_MAX - 1))]
    if drop:
        stream = getattr(gen, "stream", None)
        if stream is not None and getattr(stream, "value", None):
            cudart.cudaStreamSynchronize(stream)
        for k in drop:
            old = images.pop(k, None)
            if old is not None:
                cudart.cudaFree(old[0])
    img = text_rgba(clip_id, style, gen.seq_w, gen.seq_h)
    item = None
    if not img.isNull():
        raw = bytes(img.constBits())[: img.sizeInBytes()]
        ptr = ctypes.c_void_p()
        _check(cudart.cudaMalloc(ctypes.byref(ptr), len(raw)), "text malloc")
        _check(cudart.cudaMemcpy(ptr, raw, len(raw), 1), "text copy")
        item = (ptr, img.width(), img.height(), img.bytesPerLine())
    images[path] = item
    return item

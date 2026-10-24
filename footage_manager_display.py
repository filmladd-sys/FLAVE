# footage_manager_display.py
# Builds finished table rows for a list of footage IDs.
# Icon generation lives here.
# ASCII only.

import ctypes
import glob
import os
import sqlite3
import time
from PySide6.QtWidgets import QTableWidgetItem, QLabel
from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QImage
from PySide6.QtCore import Qt, QRect, QPoint


IMAGE_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "webp"}
VIDEO_EXTS = {"mov", "mp4", "mkv", "avi", "m4v"}
AUDIO_EXTS = {"mp3", "wav", "aac", "flac", "m4a", "ogg"}


# Global settings store (globalsettings.db): owned by global_settings.py
from global_settings import _get_global_setting


def _icon_limits():
    try:
        w = int(_get_global_setting("FootageManager_IconMaxWidthPx", "64") or "64")
    except ValueError:
        w = 64
    try:
        h = int(_get_global_setting("FootageManager_IconMaxHeightPx", "64") or "64")
    except ValueError:
        h = 64
    return max(16, w), max(16, h)


_AUDIO_EXTS = (".mp3", ".wav", ".aac", ".flac", ".m4a", ".ogg")
_AUDIO_TYPES = ("audio", "mp3", "wav", "aac", "flac", "m4a", "ogg")


def get_display_rows(project_db, id_list):
    if not project_db or not id_list:
        return []

    max_w, max_h = _icon_limits()

    conn = sqlite3.connect(project_db)
    cur = conn.cursor()

    placeholders = ",".join("?" for _ in id_list)
    cur.execute(f"""
        SELECT id, filename, original_path, file_type, size_bytes, duration
        FROM footage
        WHERE id IN ({placeholders})
    """, id_list)

    try:
        from proxy_manager import proxy_counts
        counts = proxy_counts(project_db)
    except Exception:
        counts = {}

    rows = []
    for row in cur.fetchall():
        fid, filename, path, file_type, size_bytes, duration = row
        name = filename or os.path.basename(path or "")
        ftype = (file_type or "").lower()
        size_str = _format_size(size_bytes)
        length_str = _format_length(duration)

        icon_widget = _make_icon_widget(project_db, fid, path, ftype, max_w, max_h)

        # #Proxies: count for video / image footage (clickable), empty for audio
        proxy_item = QTableWidgetItem("")
        proxy_item.setTextAlignment(Qt.AlignCenter)
        ext = os.path.splitext(path or filename or "")[1].lower()
        if ext not in _AUDIO_EXTS and ftype not in _AUDIO_TYPES:
            proxy_item.setText(str(counts.get(int(fid), 0)))
            proxy_item.setData(Qt.UserRole + 1, int(fid))
            font = proxy_item.font()
            font.setUnderline(True)
            proxy_item.setFont(font)
            proxy_item.setForeground(QColor(80, 150, 255))
            proxy_item.setToolTip("Open Proxies for this footage")
        items = [
            QTableWidgetItem(""),
            QTableWidgetItem(name),
            proxy_item,
            QTableWidgetItem(ftype),
            QTableWidgetItem(size_str),
            QTableWidgetItem(length_str),
        ]
        rows.append({"items": items, "icon_widget": icon_widget, "id": fid})

    conn.close()
    return rows


JPEG_QUALITY = 90


def _pixmap_to_jpeg_bytes(pixmap):
    # All stored icons and frames are JPEG quality 90. Alpha is not kept:
    # transparent pixels are flattened onto white first (display only).
    from PySide6.QtCore import QBuffer, QIODevice
    image = pixmap.toImage()
    if image.hasAlphaChannel():
        flat = QImage(image.size(), QImage.Format_RGB888)
        flat.fill(QColor(255, 255, 255))
        painter = QPainter(flat)
        painter.drawImage(0, 0, image)
        painter.end()
        image = flat
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "JPG", JPEG_QUALITY)
    data = bytes(buf.data())
    buf.close()
    return data


def _load_resized(conn, footage_id, max_w, max_h, frame_number=0):
    cur = conn.cursor()
    cur.execute("""
        SELECT data FROM icons
        WHERE footage_id = ? AND frame_number = ? AND is_original = 0
          AND width = ? AND height = ?
    """, (footage_id, frame_number, max_w, max_h))
    row = cur.fetchone()
    if row and row[0]:
        pix = QPixmap()
        if pix.loadFromData(row[0]):
            return pix
    return None


def _load_resized_height(conn, footage_id, target_height, frame_number=0):
    # Resized row stored at its real size whose height is target_height.
    # Rows saved by the bin are labelled with their 64x64 limits, so the
    # loaded pixmap's real height is checked too.
    cur = conn.cursor()
    cur.execute("""
        SELECT data FROM icons
        WHERE footage_id = ? AND frame_number = ? AND is_original = 0
          AND height = ?
        ORDER BY id DESC
    """, (footage_id, frame_number, target_height))
    for row in cur.fetchall():
        if row and row[0]:
            pix = QPixmap()
            if pix.loadFromData(row[0]) and pix.height() == target_height:
                return pix
    return None


def _load_original(conn, footage_id, frame_number=0):
    # Stored full frame (is_original=1, JPEG) at frame_number
    cur = conn.cursor()
    cur.execute("""
        SELECT data FROM icons
        WHERE footage_id = ? AND frame_number = ? AND is_original = 1
        LIMIT 1
    """, (footage_id, frame_number))
    row = cur.fetchone()
    if row and row[0]:
        pix = QPixmap()
        if pix.loadFromData(bytes(row[0])):
            return pix
    return None


_ICONS_DROPPED = set()   # (project_db, footage_id) whose stale icons were dropped


def _display_aspect(path, ftype, project_db=None, footage_id=None):
    # BUILD 325/326: display (w, h) of non-square-pixel video footage, else
    # None (square pixels, stills, audio, unreadable: icons exactly as
    # before). Prefers footage.pixel_aspect; probes only when missing.
    if ftype not in VIDEO_EXTS or not path:
        return None
    from clip_proxy import resolve_footage_sar, display_width
    sar = resolve_footage_sar(project_db, footage_id, path)
    if sar is None or sar[2] == sar[3]:
        return None
    return display_width(sar[0], sar[2], sar[3]), sar[1]


def _stretch_to_display(pixmap, disp):
    # BUILD 325: grabbed frame (original or proxy, coded aspect) stretched
    # to the display aspect; height kept
    h = pixmap.height()
    w = max(1, int(round(h * float(disp[0]) / float(disp[1]))))
    if w == pixmap.width():
        return pixmap
    return pixmap.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def _aspect_ok(pixmap, disp):
    # BUILD 325: True if a stored icon has the display aspect (within the
    # 1 px rounding of a resize), False for an old squeezed one
    w, h = pixmap.width(), pixmap.height()
    if w <= 0 or h <= 0:
        return True
    ar = float(disp[0]) / float(disp[1])
    return abs(w - int(round(h * ar))) <= 1 or abs(h - int(round(w / ar))) <= 1


def _drop_stale_icons(conn, project_db, footage_id):
    # BUILD 325: icons cached before BUILD 325 for non-square footage are
    # squeezed; drop that footage's icon rows once per session so they are
    # grabbed again (only this footage, no schema change). Returns False if
    # already dropped this session (then the icon found is used as is).
    key = (project_db, int(footage_id))
    if key in _ICONS_DROPPED:
        return False
    _ICONS_DROPPED.add(key)
    conn.execute("DELETE FROM icons WHERE footage_id = ?", (int(footage_id),))
    conn.commit()
    # print(f"ICON: dropped squeezed icons footage_id={footage_id}")
    return True


# Counts icon rows saved by this process. The Sequence Timeline drops its
# remembered icon misses when this changes, so a newly saved icon shows.
ICON_SAVE_SERIAL = 0


def _save_icon(conn, footage_id, pixmap, is_original, store_w, store_h, frame_number=0):
    global ICON_SAVE_SERIAL
    if pixmap is None or pixmap.isNull():
        return
    data = _pixmap_to_jpeg_bytes(pixmap)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO icons (footage_id, width, height, is_original, frame_number, data)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (footage_id, store_w, store_h, 1 if is_original else 0, int(frame_number), data))
    conn.commit()
    ICON_SAVE_SERIAL += 1


def ensure_footage_icon(project_db, footage_id, path=None, ftype=None, max_w=None, max_h=None,
                        frame_number=0, target_height=None):
    # Timing wrapper: prints total time on every return path
    t0 = time.perf_counter()
    try:
        return _ensure_footage_icon_impl(project_db, footage_id, path, ftype, max_w, max_h,
                                         frame_number, target_height)
    finally:
        pass
        # print(f"ICON: timing total ensure_footage_icon {_ms(t0)} ms")


def _ensure_footage_icon_impl(project_db, footage_id, path=None, ftype=None, max_w=None,
                              max_h=None, frame_number=0, target_height=None):
    # Public: return the stored resized icon pixmap for footage_id at
    # frame_number, creating and storing the original and resized icons
    # (is_original 1 and 0, same frame_number) if missing. Stills use frame 0.
    # With target_height (timeline), the resized icon is exactly that tall,
    # width follows aspect, and the row is stored and found by its real size.
    # Without it (Footage Manager bin), max_w/max_h limits apply as before.
    # Returns None if no icon can be made.
    if not project_db or footage_id is None:
        # print(f"ICON: return None: no project_db or footage_id (footage_id={footage_id})")
        return None
    frame_number = max(0, int(frame_number or 0))
    if target_height is not None:
        target_height = max(1, int(target_height))
        # Timeline request: height is fixed, width is left wide so aspect decides
        max_w, max_h = target_height * 4, target_height
    elif max_w is None or max_h is None:
        max_w, max_h = _icon_limits()
    # print(f"ICON: enter target_height={target_height} footage_id={footage_id} frame={frame_number} "
          # f"max={max_w}x{max_h} path={path}")
    conn = sqlite3.connect(project_db)
    try:
        if path is None or ftype is None:
            cur = conn.cursor()
            cur.execute("SELECT original_path, file_type FROM footage WHERE id = ?", (footage_id,))
            row = cur.fetchone()
            if row is None:
                # print(f"ICON: return None: footage_id={footage_id} not in footage table")
                return None
            if path is None:
                path = row[0]
            if ftype is None:
                ftype = row[1]
        ftype = (ftype or "").lower()
        disp = _display_aspect(path, ftype, project_db, footage_id)

        # Prefer an already-resized version at the current limits
        t = time.perf_counter()
        if target_height is not None:
            pixmap = _load_resized_height(conn, footage_id, target_height, frame_number)
        else:
            pixmap = _load_resized(conn, footage_id, max_w, max_h, frame_number)
        if (pixmap is not None and disp is not None and not _aspect_ok(pixmap, disp)
                and _drop_stale_icons(conn, project_db, footage_id)):
            pixmap = None
        # print(f"ICON: timing load resized lookup {_ms(t)} ms")
        if pixmap is not None:
            pass
            # print(f"ICON: hit resized {pixmap.width()}x{pixmap.height()} "
                  # f"footage_id={footage_id} frame={frame_number}")
        if pixmap is None:
            t = time.perf_counter()
            original = _load_original(conn, footage_id, frame_number)
            if (original is not None and disp is not None and not _aspect_ok(original, disp)
                    and _drop_stale_icons(conn, project_db, footage_id)):
                original = None
            # print(f"ICON: timing load full-frame lookup {_ms(t)} ms")
            if original is not None:
                pass
                # print(f"ICON: miss resized, hit stored full frame "
                      # f"{original.width()}x{original.height()}")
            if original is None:
                # print(f"ICON: miss both, grabbing full frame type={ftype} path={path}")

                t = time.perf_counter()
                if ftype in IMAGE_EXTS and path and os.path.isfile(path):
                    original = QPixmap(path)
                elif ftype in VIDEO_EXTS and path and os.path.isfile(path):
                    # BUILD 302: from the smallest usable proxy
                    # (_icon_proxy) at the matching proxy frame, else the
                    # original; full frame of that file from NVDEC, no
                    # scale, no ffmpeg. The stored full frame (is_original
                    # 1) is then that proxy's size.
                    need_h = target_height if target_height is not None else max_h
                    proxy = _icon_proxy(conn, footage_id, frame_number, need_h)
                    if proxy is not None:
                        original = _video_frame_full(proxy[0], proxy[1])
                    if original is None or original.isNull():
                        original = _video_frame_full(path, frame_number)
                    if disp is not None and original is not None and not original.isNull():
                        original = _stretch_to_display(original, disp)
                elif ftype in AUDIO_EXTS:
                    original = _speaker_icon(256, 256)
                elif not (path and os.path.isfile(path)):
                    pass
                    # print(f"ICON: path missing or not a file: {path}")
                else:
                    pass
                    # print(f"ICON: unknown type '{ftype}', nothing to grab")
                # print(f"ICON: timing grab call {_ms(t)} ms")

                if original is None or original.isNull():
                    pass
                    # print("ICON: grab result: None")
                else:
                    pass
                    # print(f"ICON: grab result: {original.width()}x{original.height()}")

                if original is not None and not original.isNull():
                    t = time.perf_counter()
                    # Video frames, stills and the speaker icon all go through
                    # _save_icon: JPEG 90, alpha flattened onto white
                    _save_icon(conn, footage_id, original, True,
                               original.width(), original.height(), frame_number)
                    # print(f"ICON: timing save original jpeg {_ms(t)} ms")
                    # print(f"ICON: saved original {original.width()}x{original.height()} "
                          # f"frame={frame_number}")

            if original is not None and not original.isNull():
                t = time.perf_counter()
                if target_height is not None:
                    scaled = original.scaledToHeight(target_height, Qt.SmoothTransformation)
                else:
                    scaled = original.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                # print(f"ICON: timing scale {_ms(t)} ms")
                t = time.perf_counter()
                if target_height is not None:
                    # Real stored size, e.g. 179x119
                    _save_icon(conn, footage_id, scaled, False,
                               scaled.width(), scaled.height(), frame_number)
                else:
                    _save_icon(conn, footage_id, scaled, False, max_w, max_h, frame_number)
                # print(f"ICON: timing save resized jpeg {_ms(t)} ms")
                # print(f"ICON: saved resized {scaled.width()}x{scaled.height()} "
                      # f"(limits {max_w}x{max_h}, target_height {target_height}) "
                      # f"frame={frame_number}")
                pixmap = scaled
    finally:
        conn.close()

    if pixmap is None or pixmap.isNull():
        # print(f"ICON: return None: no icon could be made for footage_id={footage_id} "
              # f"frame={frame_number}")
        return None
    return pixmap


def _make_icon_widget(project_db, footage_id, path, ftype, max_w, max_h):
    pixmap = ensure_footage_icon(project_db, footage_id, path, ftype, max_w, max_h)
    if pixmap is None:
        return None

    label = QLabel()
    label.setPixmap(pixmap)
    label.setAlignment(Qt.AlignCenter)
    return label


# ---- NVDEC full-frame grab -------------------------------------------------

CUDA_MEMCPY_DEVICE_TO_HOST = 2

_nvc = None            # PyNvVideoCodec module, or False if unavailable
_cudart = None         # cudart library, or False if unavailable
_decoders = {}         # path -> (file stamp, SimpleDecoder), one per file


def _icon_proxy(conn, footage_id, frame_number, need_h=None):
    # BUILD 302: (proxy path, proxy frame index) for a new video icon grab,
    # or None (grab the original). Proxies rows of the footage whose file
    # exists and that NVDEC can decode (sequence_player._proxy_decodable),
    # at the smallest percentage whose frame is at least need_h tall (the
    # icon height asked for, so the icon is never scaled up; a proxy of
    # unknown size counts as tall enough). Several modes at one percentage:
    # the player's choice (sequence_player._pick_proxy). frame_number is a
    # footage index; a rate-changing proxy (double rate, IVTC) is read at
    # the player's proxy index (proxy_frame_ratio, map_source_frame).
    try:
        rows = conn.execute(
            "SELECT percentage, COALESCE(proxy_mode, 'normal'), proxy_fps, "
            "proxy_frames, source_fps, path FROM Proxies "
            "WHERE footage_id = ? ORDER BY percentage, id",
            (int(footage_id),)).fetchall()
    except sqlite3.Error:
        return None
    from sequence_player import (_proxy_decodable, _pick_proxy, _PROXY_SIZE_CACHE,
                                 proxy_frame_ratio, map_source_frame)
    by_pct = {}
    for pct, mode, pfps, pframes, sfps, ppath in rows:
        if not ppath or not os.path.isfile(ppath) or not _proxy_decodable(ppath):
            continue
        if need_h:
            try:
                st = os.stat(ppath)
                size = _PROXY_SIZE_CACHE.get((ppath, st.st_mtime, st.st_size))
            except OSError:
                size = None
            if size is not None and size[1] < int(need_h):
                continue
        by_pct.setdefault(int(pct), []).append((mode, pfps, pframes, sfps, ppath))
    if not by_pct:
        return None
    mode, pfps, pframes, sfps, ppath = _pick_proxy(by_pct[min(by_pct)])
    index = map_source_frame(max(0, int(frame_number or 0)),
                             proxy_frame_ratio(sfps, pfps), pframes)
    return ppath, index


def _load_nvc():
    global _nvc
    if _nvc is None:
        try:
            import PyNvVideoCodec
            _nvc = PyNvVideoCodec
        except Exception:
            _nvc = False
    return _nvc or None


def _load_cudart():
    global _cudart
    if _cudart is not None:
        return _cudart or None
    names = []
    if os.name == "nt":
        cuda_path = os.environ.get("CUDA_PATH", "")
        if cuda_path:
            names += sorted(glob.glob(os.path.join(cuda_path, "bin", "cudart64_*.dll")), reverse=True)
        names += ["cudart64_12.dll", "cudart64_120.dll", "cudart64_110.dll", "cudart64_11.dll"]
    else:
        names += ["libcudart.so", "libcudart.so.12", "libcudart.so.11.0"]
    lib = None
    for name in names:
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    if lib is None:
        _cudart = False
        return None
    lib.cudaMemcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.cudaMemcpy.restype = ctypes.c_int
    _cudart = lib
    return lib


def _get_decoder(path):
    # One SimpleDecoder per path (originals and, BUILD 302, proxies); later
    # frames of the same file reuse it. A file rewritten in place (new
    # mtime or size, e.g. a regenerated proxy) gets a new decoder.
    nvc = _load_nvc()
    if nvc is None:
        return None
    key = os.path.normcase(os.path.abspath(path))
    try:
        st = os.stat(path)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        stamp = None
    entry = _decoders.get(key)
    decoder = entry[1] if entry is not None and entry[0] == stamp else None
    if decoder is not None:
        pass
        # print(f"ICON: decoder reused for {path}")
    if decoder is None:
        # print(f"ICON: decoder created for {path}")
        t = time.perf_counter()
        decoder = nvc.SimpleDecoder(
            path,
            gpu_id=0,
            use_device_memory=True,
            output_color_type=nvc.OutputColorType.RGB,
        )
        # print(f"ICON: timing decoder create {_ms(t)} ms")
        _decoders[key] = (stamp, decoder)
    return decoder


def _ms(t0):
    # Elapsed milliseconds since perf_counter value t0
    return int(round((time.perf_counter() - t0) * 1000.0))


def _video_frame_full(path, frame_number=0):
    # Timing wrapper: prints total grab time on every return path
    t0 = time.perf_counter()
    try:
        return _video_frame_full_impl(path, frame_number)
    finally:
        pass
        # print(f"ICON: timing total grab {_ms(t0)} ms")


def _video_frame_full_impl(path, frame_number=0):
    # Full native-size RGB frame at 0-based frame_number as a QPixmap.
    # NVDEC decode, cudaMemcpy device-to-host, no scaling. Returns None if
    # PyNvVideoCodec or cudart is missing or the grab fails (no ffmpeg fallback).
    frame_number = max(0, int(frame_number or 0))
    cudart = _load_cudart()
    if cudart is None:
        # print("ICON: grab failed: cudart not found")
        return None
    try:
        decoder = _get_decoder(path)
        if decoder is None:
            # print("ICON: grab failed: PyNvVideoCodec not available")
            return None
        # print(f"ICON: seek frame {frame_number}")
        t = time.perf_counter()
        decoder.seek_to_index(frame_number)
        # print(f"ICON: timing seek_to_index {_ms(t)} ms")
        t = time.perf_counter()
        batch = decoder.get_batch_frames(1)
        # print(f"ICON: timing get_batch_frames {_ms(t)} ms")
        if not batch:
            # print("ICON: grab failed: get_batch_frames returned no frames")
            return None
        frame = batch[0]
        height = int(frame.shape[0])
        width = int(frame.shape[1])
        stride = int(frame.strides[0])
        row_bytes = width * 3
        if width <= 0 or height <= 0 or stride < row_bytes:
            # print(f"ICON: grab failed: bad frame shape {width}x{height} stride={stride}")
            return None
        src = ctypes.c_void_p(int(frame.GetPtrToPlane(0)))
        host = ctypes.create_string_buffer(stride * height)
        t = time.perf_counter()
        err = cudart.cudaMemcpy(host, src, stride * height, CUDA_MEMCPY_DEVICE_TO_HOST)
        # print(f"ICON: timing cudaMemcpy {_ms(t)} ms")
        if err != 0:
            # print(f"ICON: cudaMemcpy error code {err}")
            return None
        t = time.perf_counter()
        raw = host.raw
        if stride != row_bytes:
            # Pitched rows: copy row by row into a tight buffer
            packed = bytearray(row_bytes * height)
            for y in range(height):
                packed[y * row_bytes:(y + 1) * row_bytes] = raw[y * stride:y * stride + row_bytes]
            raw = bytes(packed)
        image = QImage(raw, width, height, row_bytes, QImage.Format_RGB888).copy()
        if image.isNull():
            # print("ICON: grab failed: QImage is null")
            return None
        pix = QPixmap.fromImage(image)
        # print(f"ICON: timing QImage/QPixmap build {_ms(t)} ms")
        return None if pix.isNull() else pix
    except Exception as e:
        # print(f"ICON: grab failed: {type(e).__name__}: {e}")
        return None


def _speaker_icon(max_w, max_h):
    w = min(max_w, 64)
    h = min(max_h, 64)
    pix = QPixmap(w, h)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(200, 200, 200), 2))
    painter.setBrush(QColor(180, 180, 180))
    # body
    bx = int(w * 0.20)
    by = int(h * 0.32)
    bw = int(w * 0.22)
    bh = int(h * 0.36)
    painter.drawRect(bx, by, bw, bh)
    # cone as lines (avoids drawPolygon type issues)
    cone_x = bx + bw + int(w * 0.22)
    painter.drawLine(bx + bw, by, cone_x, by - int(h * 0.10))
    painter.drawLine(bx + bw, by + bh, cone_x, by + bh + int(h * 0.10))
    painter.drawLine(cone_x, by - int(h * 0.10), cone_x, by + bh + int(h * 0.10))
    painter.end()
    return pix


def _format_size(size_bytes):
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["KB", "MB", "GB"]
    val = float(size_bytes)
    for u in units:
        val /= 1024.0
        if val < 1024.0:
            return f"{val:.1f} {u}"
    return f"{val:.1f} TB"


def _format_length(duration):
    if duration is None or duration <= 0:
        return ""
    total = int(duration)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

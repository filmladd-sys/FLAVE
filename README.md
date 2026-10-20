# FLAVE

<!-- Intro by FilmLadd goes here -->

## Build Notes for Agentic Coders

These notes are for an AI coding agent (or a person) picking up this repo
to adapt FLAVE. They describe the code as it is now. Read them first, then
read the module headers: every .py file starts with a comment block that
says what it owns.

### 1. Stack and requirements

- **OS:** Windows. The GPU code loads Windows DLLs through ctypes
  (`nvcuda.dll`, `cudart64_*.dll`, `nvjpeg64_*.dll`, `opengl32.dll`) and
  `dark_titlebar.py` calls DWM. It will not run as-is on Linux or macOS.
- **GPU:** an NVIDIA card with NVDEC/NVENC. Device 0 is used everywhere.
- **Python 3** (the DLL loader uses `os.add_dll_directory`, so 3.8+).
- **Python packages:** there is no requirements file. The third-party
  imports are:
  - `PySide6` -- all windows, QAudioSink playback, the Sequence Player's
    QWindow + QOpenGLContext.
  - `PyNvVideoCodec` -- NVDEC decode (`SimpleDecoder`, device memory, RGB).
  - `numpy` -- optional; audio peaks, the audio mix and the WAV writer
    fall back to pure Python without it.
  - Everything else is the standard library (`sqlite3`, `ctypes`,
    `subprocess`, `wave`, ...).
- **CUDA runtime and nvJPEG DLLs:** found by `_load_cudart()` and
  `_load_nvjpeg()` in `sequence_player_gpu.py`. Search order: the `bin`
  (and `bin\x64`) folders of every `CUDA_PATH` / `CUDA_PATH_V*` environment
  variable, then `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\bin`,
  then plain DLL names on PATH. If nvJPEG is missing, preview JPEGs are
  encoded by Qt instead (slower; console prints `nvJPEG not found, using Qt`).
- **CUDA kernels:** written as PTX text (`PTX` in `sequence_player_gpu.py`)
  and loaded with `cuModuleLoadData`. No nvcc build step.
- **FFmpeg / ffprobe:** called by name, so they must be on PATH. Used for
  import probing, audio peaks / PCM extraction, proxies (NVENC) and the
  render (h264_nvenc / hevc_nvenc, with libx264 / libx265 fallback).
- **OpenGL:** only the Sequence Player window uses GL. The preview worker
  and the render composite headless (CUDA only).

**Launch:** `python project_manager.py`. `main()` prints `BUILD N` from
`counter.txt`, creates the shared CUDA context (`cuda_context.ensure_cuda`),
installs the title-bar helpers and opens the Project Manager. Closing the
Project Manager exits the app.

### 2. Module map

**App and main windows**

| File | Job |
|---|---|
| `project_manager.py` | Entry point and main window (File / Edit / Settings / Windows menus); restores open windows; prints the build number |
| `new_project.py` | New Project dialog; `_init_db` creates the project SQLite schema |
| `global_settings.py` | Global Settings window; owns the global settings DB (`_get_global_setting` / `_set_global_setting`, defaults); dark mode theme |
| `project_settings.py` | Per-project settings (project DB `settings` table) |
| `footage_manager.py` | Footage Manager window (import, sort, replace, remove) |
| `footage_manager_display.py` | Footage table rows and thumbnail icons |
| `footage_importer.py` | Adds a media file to the `footage` table (ffprobe, interlace check) |
| `sequence_manager.py` | Sequence Manager window |
| `sequence_add.py` | Add Sequence dialog |
| `undo.py` | Snapshot undo / redo in `<project>.undo.db` |
| `window_snapshot.py` | Camera button over each window's title bar; saves a PNG of that window |
| `dark_titlebar.py` | Dark title bars on Windows in dark mode |

**Sequence Timeline** (`sequence_timeline.py` is the window, mouse
handling and wiring; the `sequence_timeline_*.py` files are splits of it
whose functions take the timeline as `self` and are attached to the class)

| File | Job |
|---|---|
| `sequence_timeline.py` | Timeline window, menus, shortcuts (F5 refresh), play clock, preview retarget |
| `sequence_timeline_sql.py` | Timeline DB reads / writes, shared constants |
| `sequence_timeline_clips.py` | Clip painting, hit tests, selection, move / trim drags, clip menu |
| `sequence_timeline_tracks.py` | Track table, collapse, track menu, Track Display Properties |
| `sequence_timeline_ruler.py` | Frame ruler, scrubber, zoom |
| `sequence_timeline_audio.py` / `_video.py` / `_image.py` / `_captions.py` / `_keyframe.py` | Per-track-type helpers |
| `sequence_timeline_droppreview.py` | Drop preview boxes while dragging footage in |
| `sequence_timeline_rubber.py` | Rubber-band selection |
| `sequence_timeline_slice.py` | Slice mode (C key) |
| `sequence_timeline_link.py` | Link / Unlink decisions (pure functions) |
| `timeline_clipboard.py` | In-memory copy / cut / paste per sequence |
| `clip_drag.py`, `clip_snap.py`, `edit_points.py` | Pure math: drag deltas, snapping, transport targets |
| `clip_gain.py`, `clip_proxy.py` | Gain and proxy-percent choices for clips / footage |
| `keyframe.py` | Keyframe clip tables, dialog, evaluation |
| `markers.py`, `mile_markers.py` | Clip markers; change points of the composite (used to find what an edit changed) |
| `caption_edit.py`, `track_names.py` | Caption dialog; default track names |

**Sequence Player and GPU composite**

| File | Job |
|---|---|
| `sequence_player.py` | Sequence Player window (QWindow + GL), play ring, `timeline_snapshot`, layer list, decoders, `_build_dst` (decode + blend one frame) |
| `sequence_player_gpu.py` | CUDA / GL / nvJPEG loading, the PTX blend kernel, `_GEN_LOCK` and the GUI / worker yield rules |
| `sequence_player_preview.py` | Preview JPEG path, `composite_batch` (shared with the render), the explicit refresh and its VRAM budgeting |
| `sequence_player_audio.py` | Plays the preview audio WAV; its position is the play clock |
| `player_captions.py` | Caption overlay drawn with QPainter over the player picture |
| `cuda_context.py` | The one shared CUDA primary context |

**Preview cache and worker**

| File | Job |
|---|---|
| `sequence_preview.py` | `{sequence_id}_preview.db`: one JPEG per frame, dirty flags, the dirty-mark queue |
| `sequence_preview_worker.py` | The background thread that fills the preview DB (playhead frame, Play fill, F5 refresh) |
| `sequence_preview_gpu.py` | Headless GPU composer (no GL) for the worker and the render |
| `sequence_preview_diff.py` | Which frames an edit changed, from snapshots before / after |
| `sequence_preview_audio.py` | Audio mix into `{sequence_id}_previewaudio.wav` (no FFmpeg) |
| `peaks.py` | Waveform peaks and mono 48 kHz PCM in `<project>.audiotmp.db` |
| `refresh_preview_dialog.py` | The "Refreshing Preview" progress window |

**Proxies and render**

| File | Job |
|---|---|
| `proxy_manager.py` | Proxy files (ffmpeg / NVENC), ProxyPath, the Proxies window |
| `render_manager.py` | Render to MP4 (H.264 / H.265 + AAC, optional captions) via ffmpeg pipe |

**Data stores**

- **Project DB:** the `.db` file the user creates (schema in
  `new_project._init_db`). Footage paths are stored as absolute paths.
  Older DBs may lack newer columns; several modules check and degrade.
- **Next to the project file:** `{sequence_id}_preview.db` (preview JPEGs,
  WAL mode), `{sequence_id}_previewaudio.wav` (audio mix),
  `<project>.audiotmp.db` (source PCM), `<project>.undo.db` (undo).
- **Proxies:** in the project's `ProxyPath` setting folder (the project
  folder if unset).
- **Global settings:** a SQLite `settings` key-value table in
  `globalsettings.db`, in the same folder as the code files (ignored by
  git). `global_settings.py` owns it; every other module imports its
  `_get_global_setting` / `_set_global_setting`. On first launch the file
  is created and seeded from `GLOBAL_DEFAULTS` (dark mode off, Preview
  Frame Cap 120, JPEG quality 90, ...); existing values are never
  overwritten. State keys such as `current_project`, window geometry and
  open windows are written as the app runs.

### 3. Architecture in brief

**One frame engine.** All compositing happens on the GPU: each layer is
decoded by NVDEC (`PyNvVideoCodec.SimpleDecoder`, RGB in device memory)
or uploaded once (stills), then the PTX kernel scales, positions, crops,
color-adjusts and blends it into a sequence-size RGB buffer. The code is
`SequencePlayer._build_dst`; the headless composer in
`sequence_preview_gpu.py` borrows the player's methods, so the preview
and the render produce the same picture.

**How a frame gets on screen (the present path).** The Sequence Player
does not composite. It shows the preview JPEG for the frame:
- Scrub / still: read the JPEG from `{sequence_id}_preview.db`, decode,
  upload to the GL texture, draw a letterboxed quad. A miss asks the
  worker for that frame and keeps the last picture until it lands.
- Play: `PreviewReader` (its own thread) reads batches of JPEGs ahead of
  the playhead and decodes them with nvJPEG into device buffers; the
  timeline's play timer uses the preview audio position
  (`preview_audio_position_ms`) as the clock and shows the newest frame
  at or below the due frame. Play first runs a refresh if anything is dirty.

**Preview worker.** `sequence_preview_worker.py` runs one thread. During
normal editing it builds only the playhead frame, so scrubbing stays
realtime. Edits mark frames dirty (`sequence_preview_diff`,
`mile_markers`, queued in `sequence_preview.py` and applied on the worker
thread). F5 / Refresh Preview (`set_refresh_all`) builds every dirty or
missing frame in large card passes. The refresh's VRAM budgeting is
explained in the block comment above `_composite_by_layer` in
`sequence_player_preview.py`; read that before touching refresh sizing.

**Headless composer vs player.** There is one headless composer for the
preview (`sequence_preview_gpu._COMPOSER`), used whether or not a player
is open; the player only presents. The render creates its own private
composer (its own decoders, original footage instead of proxies).

**Threads and locks.**
- GUI thread: windows, timeline, player presenting.
- Preview worker thread, play reader thread, audio mix thread (it
  coordinates a pool of 2-3 range mix threads, `MIX_POOL_SIZE` in
  `sequence_preview_audio.py`), render thread.
- `_GEN_LOCK` (an RLock in `sequence_player_gpu.py`) is held for every
  composite. The GUI takes it first (`_GuiGen`, bumps `_gui_wants`); the
  worker waits for GUI quiet unless a refresh is running (`_worker_fast`).
- SQLite connections are per thread; preview DB writes happen on the
  worker thread.

**Shared CUDA context.** `cuda_context.py` creates one primary context at
launch; the player (GL interop), the worker, nvJPEG and the render all
use it. A CUDA fault such as error 700 (illegal address) is sticky: the
context is unusable for the rest of the process and nothing in-process
can recover it. Restart FLAVE after one. This is why the refresh exits
the program (`vram_fatal_check`) instead of letting VRAM run out.

### 4. House rules for changes

- ASCII only in code and comments. No emoji.
- Edit the module that owns the behavior (see the module map and each
  file's header). Timeline splits attach to `SequenceTimeline`; keep new
  timeline code in the matching split file.
- Keep logical changes per file small and focused.
- Increment `counter.txt` on every change and title the commit
  `BUILD N` to match. The app prints `BUILD N` from `counter.txt` at
  launch, so the console shows which build is running.
- Comments describe current behavior and the reason for non-obvious
  choices. History lives in git. (Older files still carry `BUILD N:`
  notes; convert them when you touch that code.)
- Do not reopen the proven Sequence Player present path (QWindow +
  QOpenGLContext, nvJPEG batch decode into device buffers for play,
  audio-clock pacing) unless a new effect actually breaks it.
- VRAM decisions use dedicated VRAM only (`cudaMemGetInfo` on the
  device), never shared system memory.
- No hardcoded batch sizes. The code measures the machine (free VRAM,
  real source sizes) per range; user settings like Preview Frame Cap
  are upper bounds only.

### 5. How to test

There is no test suite in the repo.

Without a GPU (any OS):
- `python -m py_compile <file>.py` on every file you touch.
- Check for non-ASCII, e.g. `grep -nP '[^\x00-\x7F]' *.py`.
- Pure modules import without Qt or CUDA and can be unit tested
  directly: `clip_snap.py`, `clip_drag.py`, `edit_points.py`,
  `sequence_timeline_link.py`, `sequence_preview_diff.py`.
- GPU code can be exercised with stub objects: import a module with fake
  `_libs()` / decoder / composer objects and call functions such as
  `refresh_plan`, `fit_refresh_pool` or `_composite_by_layer` against a
  simulated `free_vram()`. Write such stubs outside the repo.
- For comment-only changes, compare `ast.dump(ast.parse(...))` of each
  file before and after; it must be identical.

On the real Windows + NVIDIA machine: anything that decodes, composites,
plays, encodes proxies or renders. Run from a console so the log is
visible.

Useful console prefixes:
- `BUILD N` -- first line at launch.
- `WORKER preview:` -- end of a worker pass (frames, seconds, fps).
- `WORKER refresh states:` / `plan:` / `range a..b:` -- the refresh
  plan: nvJPEG state cost, free VRAM and keep, per-range N and pull size.
- `WORKER refresh pool:` -- the pool fit at each range (free after
  reserve, keep, any shortfall and what was lowered).
- `WORKER refresh batch:` -- each refresh card pass (frames, ms, fps).
- `WORKER refresh decoders:` -- the kept-decoder guard closed decoders.
- `ERROR: WORKER refresh VRAM critically low` -- the hard exit fired.
- `WORKER audio mix:`, `WORKER proxy:`, `WORKER render` -- background jobs.

### 6. Known leftovers / good first tasks

- **CUDA device 0** is hardcoded (`ensure_cuda(0)`, `gpu_id=0`).
- `REFRESH_NVDEC_HEADROOM` in `sequence_preview_worker.py` is unused.
- `_POOL_FREE` / `_log_refresh_vram` in `sequence_preview_worker.py` are
  debug-only; nothing calls `_log_refresh_vram` or sets `_POOL_FREE`.
- Three commented-out trace lines remain in `SequencePlayer._decoder`
  (`sequence_player.py`), and other files have commented-out timing
  prints.
- The header of `sequence_preview_gpu.py` still carries old history
  notes.
- When adding a global setting with a default, add it to
  `GLOBAL_DEFAULTS` in `global_settings.py` with the same value the
  getters fall back to.

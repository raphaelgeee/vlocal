# Architecture

One Python process, one `NSApplication`, a handful of threads and two kinds of
disposable subprocesses. This document explains the shape of the code and the
decisions behind it. Module by module details are in the docstrings.

## Process model

`app.py` starts pywebview, which owns the Cocoa run loop. Everything that needs
the main thread (menu bar item, global shortcut monitors, overlay panel) is
attached to that `NSApplication` from the `webview.start` callback
(`startup.after_start`). Nothing else runs on the main thread: pywebview API
methods return immediately, long work goes to daemon threads, and JavaScript is
pushed to the window with `ui_bridge._ui()`, which never waits for a result (a
synchronous `evaluate_js` on the Cocoa backend blocks the caller until the page
answers, which caused freezes).

Two subprocesses are launched from `Contents/Helpers/VlocalWorker` (a symlink to
the main binary, so PyInstaller ships one executable and macOS gives them no
Dock icon):

- `--mic-worker`: microphone capture for dictation (`micworker.py`). PortAudio
  can hang on device changes; a hang in a subprocess is killed and respawned in
  about a second without touching the main process. Frames are streamed to the
  parent as they arrive, so nothing is lost when the worker is killed.
- `--emb-worker`: voice embedding extraction for meetings (`diarizer.py`), for
  the same isolation reason and to keep the ONNX runtime out of the main
  process memory when idle.

## Module map

`app.py` only assembles: worker shims (`--mic-worker`, `--emb-worker`, before
any heavy import), the offline lock for Hugging Face, then `main()`, a sequence
of named startup steps ending in `webview.start(after_start)`. Dependencies go
one way, from `app.py` down; no module imports `app.py`, and there is no import
cycle (`tests/test_modules_app.py`).

| Module | Responsibility |
| --- | --- |
| `app.py` | entry point and startup sequence (`main`) |
| `startup.py` | `after_start` (pywebview callback): engine load, shortcut, overlay, menu bar icon, freeze watchdog, show requests, Dock reopen, update watch, quit cleanup, update confirmation and rollback notice |
| `window_mac.py` | dashboard window: creation, close-as-hide, native style and drag strip, Dock policy, WebKit cache purge after an update |
| `lifecycle.py` | the single lifecycle delegate (reopen, real quit), quit, single instance, main-thread freeze watchdog, self relaunch, wake from sleep, App Nap activities, consented auto diagnostic |
| `appstate.py` | shared process state (window, store, controller, menu bar, telemetry, API, language, model paths), read as `appstate.x` at use time |
| `applog.py` | logging setup (see Logs) |
| `settings.py` | Application Support paths, `settings.json` with an mtime cache, mode, glossary file, environment flags |
| `texts.py` | French messages and their English translations (`_te`) |
| `ui_bridge.py` | Python to UI: `_ui`, `_toast`, `_api_safe`, overlay hide timer, list and menu refresh, Settings modal |
| `engine_runtime.py` | model paths and choice, CPU threads, background engine load, memory policy ticks, reload after a model download, `--selftest` |
| `dictation.py` | `DictationController`, finalize watchdog and heartbeat, dictation supervisor, text polishing, clipboard |
| `hotkeys.py` | global shortcut wired to the controller, hot re-arm |
| `reminder_flow.py` | spoken reminders (creation, timers, re-arm at launch) and post-dictation capture to Obsidian |
| `meetings.py` | meeting state and lock, end-of-meeting pipeline (transcription, diarization, voice recognition, block attribution) |
| `api.py` | `Api`, the pywebview `js_api`, composed of `api_dictation`, `api_files`, `api_meetings`, `api_settings`, `api_system` (one base class per domain; the exposed method list is frozen by `tests/test_api_exposee.py`) |

Engine, capture and storage modules (`engine.py`, `mlx_engine.py`,
`micworker.py`, `livetail.py`, `diarizer.py`, `recorder.py`, `storage.py`...)
are unchanged and documented below.

## Logs

`applog.setup()` is called once by `main()`. In the packaged app, each module
logs to `~/Library/Logs/Vlocal/vlocal.log` (rotating, 1 MB x 5, level INFO,
`VLOCAL_LOG_LEVEL` to change it); stdout and stderr go to `console.log` in the
same folder (native messages, modules still using `print`), truncated at each
launch. In development, logs go to the console. Tests never configure logging,
so nothing is written to disk. Failures ignored on purpose (best-effort system
calls) are logged at DEBUG with their traceback: no broad `except` in the app
modules is silent (`tests/test_modules_app.py`).

## Dictation pipeline

1. The global shortcut (`hotkey_mac.py`, NSEvent monitors) or the UI calls
   `start()` on the dictation controller in `dictation.py`. Capture starts in the mic
   worker; the overlay shows the level.
2. While the user speaks, `livetail.py` watches the buffer. Past 29 s of audio it
   looks for a real silence (0.4 s under the engine's RMS threshold) after a
   24 s target and transcribes each completed window in the background. Cutting
   at silences only keeps the text identical to a single-pass transcription.
3. On release, `stop_and_finalize()` closes the microphone first (the next
   dictation can start right away), collects the live-tail windows, and
   transcribes the remainder. Chained dictations are serialized with FIFO
   tickets so text is inserted in the order of the releases.
4. The text goes through the glossary and deterministic formatting
   (`processor.py`), is copied to the clipboard and inserted at the cursor
   through the Accessibility API (`inserter.py`). Reminders are detected and
   scheduled (`reminders.py`, `notifier.py`).
5. Usage counters are updated (`storage.record_usage`), and the telemetry
   scheduler is notified.

Every step is bounded. `engine.dictation_gpu_timeout(audio_s)` gives the GPU
inference budget (22 s plus 0.5 s per second of audio); `dictation._finalize_budget_s`
adds 3 s for the finalization watchdog; a supervisor thread measures inactivity
(heartbeats), not total duration, so a slow but progressing transcription is
never cut.

## Speech engines

`engine.py` holds the CPU engine (faster-whisper, Whisper large-v3-turbo int8,
plus a small model for tight memory) and routes to `mlx_engine.py` when the
Apple GPU is available. Both run the same model: measured text output is
identical, the GPU is about seven times faster.

`mlx_engine.py` runs every GPU operation on a single worker thread because MLX
streams are per thread. A job that started and does not return within its
timeout is a Metal freeze: the worker is abandoned, a fresh one is created on
the next call, and the GPU is suspended for ten minutes while the CPU engine
takes over. Three freezes within an hour disable the GPU for the session. A
Metal error at warm-up (incompatible `metallib`) disables it immediately.

Memory is returned when idle: the GPU model is unloaded after two to fifteen
minutes without use depending on available RAM, the CPU models after one to
fifteen minutes. Reloading takes under a second and is masked by the next
dictation's speech.

## Meetings

`recorder.py` (microphone) and `meeting_visio.py` / `visio_recorder.py`
(system audio through ScreenCaptureKit, via the `syscapture` helper built from
`syscapture.swift`) write a WAV. `live_meeting.py` transcribes it in windows
during the recording; only the last window remains at stop.

`diarizer.py` extracts CAM++ 192-d embeddings (sherpa-onnx) on speech
segments, clusters them (spherical k-means; speaker count by the spectral gap of the
similarity graph, silhouette as fallback and guard, capped at one speaker per
15 s of speech, then merging of near-colinear centroids with
thresholds that depend on the amount of speech), builds the timeline and
attributes words to speakers with boundary snapping to the nearest speech
boundary. `voiceid.py` matches centroids against named voices from previous
meetings (cosine 0.75 with a margin over the second best).

## Storage and settings

SQLite (WAL) in `~/Library/Application Support/Vlocal/vlocal.db`: reminders,
dictations (if history is enabled), meetings with their word timings and
speaker blocks, glossary, daily usage counters. Settings are a JSON file in the
same folder, read through `settings._load_settings()` with an mtime cache.

## Interface

`vlocal-interface.html` is the whole dashboard: markup, CSS, an `I18N`
dictionary (French and English, checked for consistency by
`tests/check_i18n.py`) and the JavaScript that calls the Python API through
pywebview. `dictation_overlay.html` is the floating panel shown during a
dictation, hosted in a non-activating `NSPanel` (`overlay.py`).

## Updates

`updater.py` downloads the DMG announced by the `get-latest-version` edge
function (checked against its published SHA-256), verifies the Developer ID
signature, the notarization ticket and the expected version, stages a copy and
swaps it atomically on relaunch. At launch, `startup.settle_pending_update`
lets `updater.cleanup_leftovers` decide: a new version that never confirmed its
start triggers an automatic rollback (the app exits so the installer can put
the previous version back), and a version that was rolled back is never
offered again. `updater.confirm_startup` runs once the main window has loaded. `model_store.py` downloads
the speech models from the same bucket on first launch.

## Historical note

The run-loop decision (single process, pywebview as run-loop owner, menu bar
item attached with pyobjc rather than `rumps`) dates from version 6 and has held
since. Multi-process designs were rejected because the speech models would then
live in a process other than the UI's, and every transcription would cross an
IPC boundary.

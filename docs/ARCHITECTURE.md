# Architecture

One Python process, one `NSApplication`, a handful of threads and two kinds of
disposable subprocesses. This document explains the shape of the code and the
decisions behind it. Module by module details are in the docstrings.

## Process model

`app.py` starts pywebview, which owns the Cocoa run loop. Everything that needs
the main thread (menu bar item, global shortcut monitors, overlay panel) is
attached to that `NSApplication` from the `webview.start` callback. Nothing else
runs on the main thread: pywebview API methods return immediately, long work
goes to daemon threads, and JavaScript is pushed to the window with `_ui()`,
which never waits for a result (a synchronous `evaluate_js` on the Cocoa
backend blocks the caller until the page answers, which caused freezes).

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

## Dictation pipeline

1. The global shortcut (`hotkey_mac.py`, NSEvent monitors) or the UI calls
   `start()` on the dictation controller in `app.py`. Capture starts in the mic
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
   (`processor.py`). If local intelligence is on and the dictation carries a
   declared intent (see below), it is formatted by the model, under a time
   budget and guardrails; otherwise it is untouched. It is then copied to the
   clipboard and inserted at the cursor through the Accessibility API
   (`inserter.py`). Reminders are detected and scheduled (`reminders.py`,
   `notifier.py`).
5. Usage counters are updated (`storage.record_usage`), and the telemetry
   scheduler is notified.

Every step is bounded. `engine.dictation_gpu_timeout(audio_s)` gives the GPU
inference budget (22 s plus 0.5 s per second of audio); `app._finalize_budget_s`
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

## Local intelligence (preview)

Three layers, and the model only in the last one.

1. **Recognition** (`intents.py`, no model, under a millisecond). A request is
   a named deliverable (mail, prompt, message, note, liste, post) or a modifier
   (a language, "plus court", "plus formel") found at the edge of the
   dictation: in its first words (declarative "c'est un mail pour Marie :",
   objective "l'objectif c'est un prompt", imperative "rédige-moi un mail",
   polite "tu peux me faire un message", conversion "mets ça en liste") or as
   its last clause, detached by punctuation ("…, mets ça en liste"). Never in
   the middle of a sentence: "je t'envoie ça en mail demain" is not a request.
   Two modes come out of it: FORMAT (the dictation is the text) and COMPOSE (the
   dictation describes what to write). A definite article ("fais la liste des
   participants") or a human-directed follow-up ("… et envoie-le à Paul") means
   a task for a person. When the frontmost app is an AI assistant, a terminal
   or a code editor (`contexte_assistant`, bundle id and window title captured
   at the start of the dictation), COMPOSE requests are for that app and are
   ignored; announced formats still apply.
2. **Formatting** (`mise_en_forme.py`, no model). FORMAT requests for an email,
   a message, a note, a list or a post are built from the dictated words:
   oral hesitations removed, sentences split at their connectors, bullets,
   greeting and closing chosen by "tu" or "vous", signature appended.
3. **Writing** (`assist.py`, model). Prompts, COMPOSE requests, translations,
   shorter or more formal versions, replies to copied text. One instruction
   per deliverable and mode, one short example given as a real exchange,
   output length capped per deliverable, time budget sized to it and doubled
   in Low Power Mode. `accepte_demande()` rejects a copy of the request, an
   instruction leak, a meta answer ("voici", "je vais"), a looping list, a
   language or register switch, missing key words or proper names of the
   request, and, in FORMAT mode, lost content. Knowledge requests (a shopping
   list for a raclette, "explique…") are not sent to the model at all.

- **Reminders.** A second pass only when `reminders.is_reminder()` said no and
  a cheap cue ("rappelle", "oublie", "remind"...) is present: the model returns
  `{titre, quand}` as JSON, `resoudre_rappel()` computes the date with the
  deterministic parser (English phrases mapped first, spelled hours turned into
  digits) and falls back to the whole sentence. No date, no reminder.
- **Meetings.** A one-hour transcript is read in chunks of about 9,000
  characters; each chunk yields JSON (decisions, actions, open topics, first
  names a speaker gives or is called by). Chunks are merged and rendered by
  Python, not by the model. First names are offered as suggestions in the UI
  and applied through the usual `rename_speaker`.
- **Lifecycle.** The model (Qwen3-4B-Instruct-2507, 4-bit, 2.3 GB) lives in the
  optional section of the R2 manifest (`model_store.download_optional`). It is
  loaded only when a writing request arrives, or at the start of a dictation
  if it was used in the last fifteen minutes, never below 3.2 GB of free
  memory, and unloaded after ten minutes idle by the watchdog that unloads
  Whisper. The settings card is shown only where the preview flag is set
  (`com.vlocal.identity preview_assist = 1`, or `VLOCAL_PREVIEW=assist`).
  `Vlocal --assist-selftest` is a build gate: the packaged app must import
  mlx-lm without torch and, if the model is cached, write a prompt.

## Storage and settings

SQLite (WAL) in `~/Library/Application Support/Vlocal/vlocal.db`: reminders,
dictations (if history is enabled, with the raw text next to a formatted
one), meetings with their word timings, speaker blocks and an optional report,
glossary, daily usage counters. Settings are a JSON file in the
same folder, read through `_load_settings()` with an mtime cache.

## Interface

`vlocal-interface.html` is the whole dashboard: markup, CSS, an `I18N`
dictionary (French and English, checked for consistency by
`tests/check_i18n.py`) and the JavaScript that calls the Python API through
pywebview. `dictation_overlay.html` is the floating panel shown during a
dictation, hosted in a non-activating `NSPanel` (`overlay.py`).

## Updates

`updater.py` downloads the DMG announced by the `get-latest-version` edge
function, verifies the Developer ID signature and the notarization ticket,
stages a copy and swaps it atomically on relaunch. `model_store.py` downloads
the speech models from the same bucket on first launch.

## Historical note

The run-loop decision (single process, pywebview as run-loop owner, menu bar
item attached with pyobjc rather than `rumps`) dates from version 6 and has held
since. Multi-process designs were rejected because the speech models would then
live in a process other than the UI's, and every transcription would cross an
IPC boundary.

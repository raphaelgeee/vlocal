# Vlocal

Voice dictation and meeting transcription for macOS, running entirely on your Mac.

Hold a shortcut, speak, release: the text is written where your cursor is, in any
application. Record a meeting and get a transcript with who said what. Nothing is
uploaded: the speech models run on the Apple Silicon GPU (or CPU), and your audio,
transcripts and voice prints never leave the machine.

Vlocal is free and open source (AGPL-3.0). It was a paid product from June to
August 2026; since version 1.1.0 (September 2026) the paywall is gone.

Français : [README.fr.md](README.fr.md).

## What it does

- **Dictation at the cursor.** Default shortcut: hold `Ctrl + Cmd`, speak, release.
  Works in every app, including ones without a text field focused (the text is
  then copied to the clipboard). Long dictations are transcribed while you speak,
  so the wait at release stays around one second whatever the length.
- **Meetings.** Records the microphone (and optionally the system audio of a video
  call), transcribes incrementally during the meeting, then separates speakers
  with local voice prints. Named speakers are recognized in later meetings.
- **Reminders** parsed from natural French ("rappelle-moi demain à 9h de ...") and
  delivered as native macOS notifications.
- **Glossary** of proper nouns and domain terms, applied to every transcription.
- **Obsidian** connector: dictations can be filed in an existing vault, or Vlocal
  creates a "Vlocal, ma voix" vault where dictations, meetings (by speaker) and
  reminders are kept as Markdown, with a CLAUDE.md describing the layout for an
  assistant working on your files.
- Interface in French and English.

## Requirements

- Mac with Apple Silicon (M1 or later). Intel Macs are not supported.
- macOS 11 or later. The GPU engine needs macOS 14 or later; on older systems the
  CPU engine is used automatically (same model, slower).
- About 3 GB of disk for the models, downloaded on first launch.

## Install

Download the signed and notarized DMG from [vlocal.org](https://www.vlocal.org),
open it, drag Vlocal to Applications. On first launch the app downloads the
speech models, asks for the Microphone and Accessibility permissions, and asks
your first and last name (see "Data" below).

## Data: what leaves your Mac, and what never does

Never: audio, dictated text, meeting content, transcripts, voice prints, file
names, email address, machine name. All of that stays on disk, in
`~/Library/Application Support/Vlocal`.

With your agreement (asked once at install, changeable in Settings > Shared
data), the app sends once a day:

| Field | Purpose |
| --- | --- |
| a random install identifier (UUID) | count installs, no link to the machine |
| first name, last name, as typed | know who uses Vlocal |
| Vlocal version, macOS version | support |
| per day: number of dictations, number of words, estimated time saved | measure real use |

Nothing else. The exact payload is built in [telemetry.py](telemetry.py)
(`build_rows`) and covered by [tests/test_telemetry.py](tests/test_telemetry.py),
which fails if any other field is added. The estimated time saved uses the same
formula as the dashboard: words at 40 words per minute typed versus 150 spoken.

Two other network calls exist: a version check (`get-latest-version`, no
personal data) and the "Send a diagnostic" button in Settings, which sends the
message you type and, if you fill it, your email.

## How it works

| Component | Implementation |
| --- | --- |
| Speech to text | Whisper large-v3-turbo. GPU path: [mlx-whisper](https://github.com/ml-explore/mlx-examples) with 8-bit weights. CPU path: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) int8, plus a small model for low memory. |
| Voice activity | Silero VAD (through faster-whisper) and an RMS gate shared by the live-tail. |
| Speaker separation | [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) CAM++ 192-d embeddings, spherical k-means, speaker count by spectral gap (silhouette as fallback) with a duration cap, boundary snapping to speech. |
| Text formatting | Deterministic rules ([processor.py](processor.py)); no generative model, nothing is rewritten. |
| Storage | SQLite in WAL mode ([storage.py](storage.py)). |
| UI | A single HTML file ([vlocal-interface.html](vlocal-interface.html)) in a pywebview window, a floating dictation overlay, a menu bar item. |
| Reliability | The microphone is captured in a disposable subprocess that is killed and respawned if it hangs; GPU inference has a proportional timeout and a temporary CPU fallback; a supervisor bounds every step of a dictation. |

More in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Run from source

```bash
git clone https://github.com/raphaelgeee/vlocal.git
cd vlocal
python3.12 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python download_whisper.py     # CPU models (faster-whisper)
./venv/bin/python download_mlx.py         # GPU model (MLX), Apple Silicon
./venv/bin/python app.py
```

The `mlx-metal` wheel must target macOS 14 or the GPU path fails on users
running anything older than the build machine. `build_app.sh` checks this
(step 3ter) before producing a DMG.

Dictation inserts text through the Accessibility API. macOS grants that
permission to a specific binary: when running from source, grant it to your
Python interpreter, and grant it again after rebuilding the app.

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -p "test_livetail.py"
./venv/bin/python -m unittest discover -s tests -p "test_gpu_resilience.py"
./venv/bin/python -m unittest discover -s tests -p "test_telemetry.py"
./venv/bin/python -m unittest discover -s tests -p "test_speaker_count.py"
./venv/bin/python tests/test_processor.py
./venv/bin/python tests/test_reminders.py
./venv/bin/python tests/test_storage.py
./venv/bin/python tests/test_vocal_commands.py
./venv/bin/python tests/test_diarization_v18.py
```

The speaker-separation quality bench (`tests/rd`) runs on real meeting
recordings and is not published.

## Build the app

`./build_app.sh` produces `dist/Vlocal.app` and `dist/Vlocal-<VERSION>.dmg`
(PyInstaller, ad hoc signature). `notarize.sh` signs with a Developer ID and
notarizes with Apple. Publishing to the update channel is described in
[docs/RELEASING.md](docs/RELEASING.md); it requires the project's Cloudflare R2
bucket and Supabase project, so only the maintainer can ship an update to
existing users. Forks can point [supabase_config.py](supabase_config.py) to
their own backend ([docs/SUPABASE.md](docs/SUPABASE.md)).

## Project layout

```
app.py                 application: pywebview API, dictation controller, meetings, startup
engine.py              Whisper engine (CPU), audio capture, adaptive routing, GPU dispatch
mlx_engine.py          GPU backend (MLX), single worker thread, timeouts, CPU fallback policy
livetail.py            transcription of long dictations while speaking
micworker.py           disposable microphone subprocess
diarizer.py            speaker separation (embeddings, clustering, timeline)
voiceid.py             known voices across meetings
live_meeting.py        incremental meeting transcription
recorder.py, meeting_visio.py, visio_recorder.py   meeting audio capture
processor.py           deterministic text formatting and glossary
reminders.py           French reminder parser
storage.py             SQLite persistence
telemetry.py           declared usage data (see "Data")
supabase_config.py     backend coordinates (public anon key)
updater.py, model_store.py   in-app updates, model download
overlay.py, menubar.py, hotkey_mac.py, inserter.py, clipboard.py, notifier.py, permissions.py, obsidian.py
vlocal-interface.html  dashboard; dictation_overlay.html  overlay
Vlocal.spec, build_app.sh, notarize.sh, entitlements.plist   packaging
supabase/              SQL migrations for the backend tables
tests/                 unit tests and fixtures
```

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

AGPL-3.0, see [LICENSE](LICENSE). A commercial license is available for
companies that want to integrate Vlocal without the AGPL obligations:
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

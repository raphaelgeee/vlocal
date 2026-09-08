# Changelog

Dates are publication dates on the update channel. Earlier internal versions
(3.x, June 2026) are not listed.

## 1.1.2, 8 September 2026

Security review before the public announcement.

- In-app updater: the downloaded update must be signed by this project's Apple
  team (Team ID pinned), in addition to the Developer ID and notarization
  checks. A notarized app from another developer is refused.
- Backend: public roles hold no table privilege at all; the telemetry function
  is the only surface open to the app's public key (300 new installs per hour,
  sanitized names, capped values); feedback endpoint rate-limited; paid-era
  functions answer 410; admin console served with a Content-Security-Policy.

## 1.1.1, 7 September 2026

Telemetry transport fixed. 1.1.0 wrote its daily counters with a direct
PostgREST upsert; under row-level security that upsert needs read access to the
row, which would have exposed names to anyone holding the public key. The app
now calls a single `security definer` function (`vlocal_report_usage`) and the
tables are not readable or writable with the public key at all. Nothing else
changes.

## 1.1.0, 6 September 2026

Free and open source.

- The paywall, license keys and payment flow are removed. The first-launch
  screen asks for a first and last name instead, with a clear statement of what
  the app sends and a "continue without sharing" option.
- Declared telemetry (`telemetry.py`): install identifier, names, versions, and
  per-day counters (dictations, words, estimated time saved). Nothing else.
  Locked by a test.
- Speed regression fixed. One GPU inference exceeding a fixed 22 s timeout
  switched the whole session to the CPU engine (measured: 1.0 s per dictation on
  GPU, 8.0 s on CPU, 1.5 GB more memory). The timeout is now proportional to the
  audio, the switch is a 10-minute suspension, and only three freezes in an hour
  disable the GPU for the session.
- Live-tail fixed: the search for a silence to cut a window was limited to 16 s
  after the target and never widened, so a long sentence stalled it for the rest
  of the dictation. It now searches all available audio, incrementally.
- Finalization watchdog proportional to the dictation length (was a fixed 25 s).
- Meetings: automatic speaker count by spectral gap (eigengap of the voice
  similarity graph) with the silhouette as fallback and guard. On a real
  69-minute meeting with four speakers the silhouette hesitated between 2 and 4
  (0.296 versus 0.289) and chose 2; the spectral estimate finds 4, keeps 2 on
  five two-speaker meetings and 1 on single-voice subsets. Region-level speaker
  accuracy on the annotated windows: 81.6 % to 94.0 % (same as forcing 4).
- Dictation overlay: the pointer now sits on the menu bar edge on every screen.
  The previous offset assumed a 37 px menu bar and pushed the tip 4 px under
  the bar on 14-inch notch Macs (33 px), entirely under it on 13-inch Macs.
- Obsidian: a "Vlocal, ma voix" vault can be created from Settings. Dictations
  go to a daily note, each meeting gets a note with speakers, reminders are
  listed; a CLAUDE.md at the root describes the structure for an assistant.
- Admin console: token stored as a SHA-256 hash, per-IP and global lockouts.
- Meeting import: GPU timeout proportional to the block length (was 60 s).
- Code: live-tail extracted to `livetail.py`, backend coordinates centralized in
  `supabase_config.py`, Nuitka compilation removed from the build, usage
  counters table (`usage_days`) in SQLite.

## 1.0.26, 16 August 2026

Meetings: a short recording with a single speaker was split into several
speakers. Centroid merging thresholds now adapt to the duration of speech, and
the detected speaker count is capped by the available speech (about 15 s per
speaker).

## 1.0.25, 15 August 2026

- Meeting re-analysis: speaker boundaries snapped on all three attribution
  paths (the fallback path was missing it), a visible warning when the
  diarization falls back to the tile path, progress bar.
- Helper processes launched from `Contents/Helpers` so they no longer show a
  Dock icon.

## 1.0.24, 15 August 2026

Speaker boundary snapping wired into the production attribution path (1.0.23
had it on the fallback path only). Measured word-level speaker accuracy on the
annotated bench: 92.3 % to 96.9 %.

## 1.0.23, 14 August 2026

Meetings: word-to-speaker attribution snapped to the nearest speech boundary
(up to two words), known-voice matching ignores placeholder names, match
threshold raised to 0.75.

## 1.0.22, 13 August 2026

Dictation microphone capture moved to a disposable subprocess that is killed
and respawned in about one second if it hangs; chained dictations (a new one can
start while the previous one is being transcribed) with FIFO insertion order;
no user is blocked more than two seconds by the microphone.

## 1.0.21, 23 July 2026

Stability release: microphone auto-heal, finalization heartbeat, GPU freeze
telemetry.

## 1.0.13 to 1.0.17, 24 to 26 June 2026

Obsidian connector, "à la ligne" voice command, in-app diagnostic report.

## 1.0.1 to 1.0.12, 17 to 21 June 2026

Launch series: in-app updater with signature verification, notarization,
hardened runtime entitlements, reminders as native notifications, window drag
fix, welcome and permission flows.

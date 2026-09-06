# Releasing

This is the maintainer's runbook. It needs the project's Cloudflare R2 bucket
(`r2:vlocal/`), the Supabase project and an Apple Developer ID. Forks that want
their own channel: see SUPABASE.md and point `supabase_config.py` and
`updater.py` to their own endpoints.

## 1. Version

`VERSION` (read by `Vlocal.spec`) and `APP_VERSION` in `app.py` must match.
Bug fix: +0.0.1. Feature: +0.1.0. Add the entry to `CHANGELOG.md`.

## 2. Build

```bash
./build_app.sh                 # full: quality, speed and RAM gates, then PyInstaller
VLOCAL_SKIP_GATES=1 ./build_app.sh   # UI-only change, engine untouched
```

The build is heavy (several GB of RAM at the gates). Step 3ter refuses a DMG
whose `mlx-metal` library targets a macOS newer than 14: that mistake ships a
GPU that fails on every user with an older macOS, silently falling back to CPU.

Output: `dist/Vlocal.app`, `dist/Vlocal-<VERSION>.dmg`.

## 3. Notarize

```bash
export DEV_ID="Developer ID Application: <name> (<team id>)"
NOTARY_PROFILE=<keychain profile> bash notarize.sh
```

`notarize.sh` signs a clean copy outside iCloud (Finder metadata breaks
`codesign --strict`), submits the DMG from `/tmp` (iCloud upload saturates the
link otherwise), staples the ticket and copies the final DMG back to `dist/`.
Check `xcrun stapler validate dist/Vlocal-<VERSION>.dmg` before uploading: an
unstapled DMG blocks the first offline launch.

## 4. Verify the artifact

- `defaults read dist/Vlocal.app/Contents/Info.plist CFBundleShortVersionString`
  is the new version.
- `strings` on the compiled modules confirm the fix is in the binary, not only in
  the source (a build once recompiled a stale copy).
- Install the DMG on a clean user account if the change touches permissions.

## 5. Upload, one version on R2

```bash
rclone copy dist/Vlocal-<VERSION>.dmg r2:vlocal/
rclone lsl r2:vlocal/ | grep dmg
rclone delete r2:vlocal/Vlocal-<PREVIOUS>.dmg      # keep exactly one DMG
```

The bucket also holds the model files (`models/`, `models_manifest.json`).
Never delete those: every fresh install downloads them.

## 6. Publish on both channels

Open `https://www.vlocal.org/admin`, tab "Publier": version, DMG URL, DMG size
in bytes, and the notes. Notes start with the date ("Mise à jour du 6 septembre
2026"), describe what changed for the user, and end with what did not change
(quality, speed, everything local). The button writes `app_versions` (read by
the in-app updater through `get-latest-version`) and the site download URL
(`app_config.download_url`).

Check: `curl -s https://<project>.supabase.co/functions/v1/get-latest-version
-H "apikey: <anon>"` returns the new version and length.

## 7. Tell users

Never email users without the maintainer's explicit approval of the exact text
and recipient list.

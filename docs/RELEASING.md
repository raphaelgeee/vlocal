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

If the keychain profile is missing ("No Keychain password item found for
profile"), do not debug the keychain: pass the App Store Connect API key
directly, `NOTARY_KEY=<path to AuthKey_XXXX.p8> NOTARY_KEY_ID=<key id>
NOTARY_ISSUER=<issuer uuid> bash notarize.sh`. The profile vanished twice on
the same day once; the key file mode has no such dependency.

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

Two channels, and the app does the first one by itself.

**In the app.** Once `app_versions` carries the new version, every running Vlocal
notices within six hours (or at its next launch): a dot on the Updates entry of
the sidebar, a banner at the top of the window, and an "Update x.y.z available"
entry at the top of the menu bar menu. Nothing to do here, but check it once:
open the Updates tab of an older build, it must show the new version.

**By email.** One email per version, rendered from a short spec, sent by the
maintainer from their own mailbox after reading it.

1. Write `mail/<version>.json` (copy the previous one). Fields: subject, preview
   text, the three headline lines, the version, the uppercase subtitle, one
   paragraph, three cards (title and text), the download link and the hero
   image URL (`https://www.vlocal.org/mail/pastille-enregistrement@2x.png` or
   `pastille-resultat@2x.png`). Plain words: which bugs are fixed, which
   features are new, in the user's language. No em-dashes.
2. Render and list the recipients:

   ```bash
   ./venv/bin/python tools/release_mail.py mail/<version>.json --destinataires
   ```

   The HTML lands in `dist/mail-<version>.html`. Open it in a browser and read
   it once as a user would. Images are hosted on vlocal.org (`/mail/*.png`),
   never embedded: Gmail clips messages above 102 KB.
3. Recipients come from the admin console (installations that left an email)
   plus the former paying users kept in `_private/` (never committed). Addresses
   at `miria.ai` are excluded by the tool. Remove anyone who asked not to be
   written to.
4. Send from the maintainer's mailbox, recipients in Bcc, the rendered HTML as
   the body, subject as in the spec. Send once. Group versions when several ship
   the same week: one email, the newest version in the title.

Never email users without the maintainer's explicit approval of the exact text
and recipient list. Nothing in this repository sends email.

# Security

## Reporting a vulnerability

Write to support@vlocal.org. Please do not open a public issue for a
vulnerability before it is fixed. You will get an answer within a week.

## Scope

- The macOS application (this repository).
- The update channel: DMG hosted on Cloudflare R2, version metadata served by
  the `get-latest-version` edge function. The in-app updater verifies the
  Developer ID signature and Apple notarization of the downloaded DMG before
  installing it.
- The telemetry endpoint (Supabase tables `installs` and `usage_days`). Writes
  are allowed with the public anon key; reads are not. See
  `supabase/migrations`.

## What the app does not do

- No account, no password, no payment.
- No audio or text is transmitted (see "Data" in the README).
- No code is downloaded or executed at runtime other than signed app updates
  that the user starts from the Updates tab.

# Contributing

Thank you for taking the time. Vlocal is maintained by one person; small,
focused pull requests are the ones that get merged.

## Before you start

- Open an issue for anything beyond a bug fix, so the direction can be agreed
  before the work.
- The two things users notice are transcription quality and the wait at release.
  A change that trades either of them for something else needs numbers.
- Everything stays local. A change that sends new data anywhere is out of scope
  unless it is discussed first and declared in the README table.

## Development setup

See "Run from source" in the README. Python 3.12, Apple Silicon.

## Rules of the codebase

- Python: readable over clever, comments in French are fine (most of the code
  is), no new dependency without a reason written in `requirements.txt`.
- User-facing text: French and English, no em dash (use a comma, a colon or a
  parenthesis), no emoji in the app.
- The dashboard is one HTML file. Add strings to both `fr` and `en` blocks of
  `I18N`; the build fails a consistency check otherwise (`node` parses the
  dictionary and compares keys).
- Never block the main thread: pywebview calls must return quickly, long work
  goes to a thread, JS is pushed with `_ui()` (fire and forget).
- Any timeout must be proportional to the audio it covers or documented as a
  hard bound on a failure case (see `engine.dictation_gpu_timeout`).

## Tests

Run the commands listed in the README. Add a test when you fix a bug that a
test could have caught: `tests/test_livetail.py` and
`tests/test_gpu_resilience.py` are small examples of the expected style.

## Commit messages

One change per commit, an imperative first line, and a body that says why.

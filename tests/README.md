# Musicdrome test suites

Two suites, both driven by Playwright:

| Suite | Files | What it covers |
|---|---|---|
| **E2E** | `e2e/auth.spec.ts`, `library.spec.ts`, `playback.spec.ts`, `playlists.spec.ts`, `features.spec.ts` | The web UI in a real browser — sign-in and multiuser separation, browsing, actual audio playback, playlists, discovery, podcasts, settings, admin, dark mode |
| **Subsonic conformance** | `e2e/subsonic.spec.ts` | The `/rest` API at the request level: both auth mechanisms, XML/JSON/JSONP envelopes, Subsonic error codes, and every verb the server implements |

## Running locally

```bash
cd tests
npm install
npx playwright install chromium   # skip if your image already ships one
npm test
```

Playwright starts its own throwaway server via `run-test-server.sh`. That script:

1. wipes `tests/.tmp/{config,cache,podcasts,downloads}` so every run starts clean,
2. generates a deterministic library with `seed_library.py` — 19 tagged WAV
   tracks across 5 albums by 4 artists, synthesised rather than committed as
   binaries,
3. indexes it with `musicdrome.cli scan`,
4. serves the app on `127.0.0.1:4599` with AI, Last.fm, MusicBrainz and Lidarr
   switched off so no test ever depends on a third-party service.

Useful flags:

```bash
npm test -- e2e/subsonic.spec.ts       # one file
npm test -- --grep "smart playlist"    # one test by name
npm run test:headed                    # watch it drive the browser
npm run test:ui                        # interactive runner
npm run report                         # open the last HTML report
```

## Running against a container

```bash
docker compose --profile test run --rm playwright
```

Compose sets `E2E_BASE_URL`, which makes the config skip its own server and
target the running `musicdrome` service — so the suite exercises the image you
would actually deploy. The service must be healthy first (`docker compose up -d`).

## Prerequisites

The frontend must be built before the E2E specs will find a UI:

```bash
cd frontend && npm install && npm run build
```

The container image builds it as part of `docker build`, so the compose path
needs no extra step.

## Notes on the design

- **One worker, serial.** The suite mutates a single shared library and account;
  parallel workers would race on play counts and playlist contents.
- **Real audio.** Playback tests assert that `<audio>` is unpaused and its clock
  is advancing, which means bytes genuinely flowed through `/stream`.
- **Fake ffmpeg is not stubbed.** If ffmpeg is missing, the server serves the
  original file instead of failing, and the tests still pass — that fallback is
  itself covered.
- **`data-testid` over text selectors** wherever a string appears twice (a card
  and its success toast, a nav item and a stat tile).

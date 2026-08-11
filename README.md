# Musicdrome

AI music discovery that downloads what it recommends.

Musicdrome reads what you already listen to from Last.fm and ListenBrainz, asks
an AI what you would like next, scores every suggestion with a match percentage,
and downloads the ones you pick as tagged MP3s at 320 kbps.

Single container: FastAPI plus vanilla HTML, CSS and JavaScript. No database
server, no build step, no telemetry, no third-party embeds. Dark only.

```
   your scrobbles          one AI call            you decide
 Last.fm ─┐                     │                      │
          ├──▶ SQLite ──▶ 40 ranked tracks ──▶  ↓ download  ♥ save  ✕ hide
 ListenBrainz ┘           + match % + why            │
                                                     ▼
                              YouTube Music ─▶ yt-dlp ─▶ MP3 320
                                                     │
                                MusicBrainz tags, cover art, and
                                Artist/Album/01 - Title.mp3
```

## Quick start

No clone, no build. Two files and a `docker compose up`:

```bash
mkdir musicdrome && cd musicdrome
curl -O https://raw.githubusercontent.com/dean1850/musicdrome/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/dean1850/musicdrome/main/.env.example
# fill in .env, then
docker compose up -d
```

Open <http://localhost:3046> and press **Scan now**.

Update with `docker compose pull && docker compose up -d`. The database survives
schema changes automatically.

## What you need

**A listening history.** Either is enough, both is fine:

- **Last.fm** — a free [API key](https://www.last.fm/api/account/create) and your
  username. Musicdrome only reads; it never scrobbles, so it needs no password
  and no API secret. Whatever you already listen with keeps scrobbling as it does.
- **ListenBrainz** — just your username, for a public profile.

**An AI backend.** One of:

| `AI_PROVIDER` | Needs | Notes |
|---|---|---|
| `ollama` | A running Ollama | Local and free. One call per scan means an 8B model is genuinely fine here. |
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `openai` | `OPENAI_API_KEY` | Also works with LM Studio, vLLM or anything OpenAI-compatible via `OPENAI_BASE_URL`. |

## How a scan works

1. **Sync.** New scrobbles are pulled from every configured source. Each keeps
   its own cursor, so only new plays are fetched.
2. **Profile.** Your most-played artists, most-played tracks and most recently
   discovered artists over the listening window.
3. **Ask.** One AI call returns the whole batch — 40 tracks by default, each with
   an artist, a title, a match percentage and a one-line reason naming what in
   your history led there.
4. **Resolve.** Each answer goes to MusicBrainz for the canonical artist, album,
   year and recording length, then to Last.fm for cover art and genre tags.
5. **Filter.** Anything you already have is dropped — see below.
6. **Show.** What survives becomes cards you can download, save or hide.

Downloads then search YouTube Music through `ytmusicapi`, score candidates on
artist, title and duration against the MusicBrainz recording, and fall back to a
plain YouTube search when YouTube Music does not carry the track. A card that
cannot be matched confidently is marked failed rather than guessed at — a wrong
file in your library is worse than a missing one.

## What it will never suggest

A track is excluded if any of these is true:

- it is anywhere in your scrobble history
- Musicdrome already downloaded it
- you dismissed it with ✕
- it was found in `EXCLUDE_MUSIC_DIR`

That last one is an existing library you point at. It is mounted read-only, and
only artist and title tags are read from it — no library database is built,
nothing is moved, nothing is written. Set it if you have a collection that
predates Musicdrome.

Matching is done on normalised keys, so "The Beatles" and "Beatles", or
"Karma Police" and "Karma Police (Remastered 2016)", are recognised as the same
thing rather than suggested back to you.

## Configuration

`.env` carries startup concerns only — keys, usernames, paths, port. Every value
has a working default, so a `.env` with your Last.fm key and an AI credential is
a complete configuration. See [`.env.example`](.env.example).

Everything you would want to change while using it lives in the **Settings** tab
and applies immediately, no restart:

| Setting | Default | |
|---|---|---|
| Schedule | Daily | Off, every 6 hours, daily or weekly |
| Tracks per scan | 40 | 5–100, one AI call regardless |
| Listening window | 90 days | How far back the taste profile reaches |
| Auto-download | Off | Download anything above a match threshold |
| Threshold / daily cap | 85% / 25 | The cap counts finished downloads in 24 hours |
| Hide below | 0% | Display filter for the card grid |
| Retention | 60 days | Un-actioned cards are purged; decisions are kept |
| Taste summary | On | One AI call a day for the stats page |

## Downloads

Always MP3 at 320 kbps — there is no format setting to get wrong. Files are
tagged from MusicBrainz (artist, title, album, album artist, year, track number,
recording MBID) with cover art embedded, and filed as:

```
/music/Radiohead/OK Computer/06 - Karma Police.mp3
/music/_playlists/musicdrome-scan-0007.m3u
```

Each scan writes an `.m3u` of its own batch with relative paths, so a discovery
run can be played as a set and the folder can be moved without breaking it.

Jellyfin, Navidrome, Plex and friends read this layout as-is — point them at the
same directory.

## The stats tab

Computed straight from your scrobbles in SQLite, so it costs nothing to open and
works with the AI switched off: top artists and tracks, plays per day, a
listening clock in your own timezone, and how much of your listening is new
versus familiar. The one AI touch is a short written summary of your taste,
refreshed once a day and cacheable to zero calls by switching it off.

## Security

Musicdrome has **no authentication**, by design — it is meant for a trusted home
network. Put it behind a VPN or an authenticating reverse proxy if you need to
reach it from outside.

## Notes

- MusicBrainz asks for one request per second per client and Musicdrome honours
  it, so enriching a 40-track scan takes about a minute. It runs in the
  background; you can keep using the UI.
- If YouTube starts asking for a sign-in, point `YTDLP_COOKIES_FILE` at an
  exported cookies file.
- You are responsible for complying with copyright law and the terms of service
  of the sources you download from, wherever you are.

## License

GPL-3.0.

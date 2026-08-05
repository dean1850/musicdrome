# Musicdrome

A self-hosted music server built as a Navidrome replacement — the same Subsonic
API your existing clients already speak, plus AI-curated smart playlists,
listening analytics, podcasts, Lidarr sync and automated track acquisition.

Dark mode only. Every setting lives in `.env`. Runs as a single container under
Docker Compose.

---

## Features

| | |
|---|---|
| **Subsonic API** | v1.16.1 at `/rest` with OpenSubsonic extensions. Works with DSub, Substreamer, play:Sub, Symfonium, Sonixd, Feishin and other Airsonic-compatible clients. XML, JSON and JSONP envelopes; both plaintext and salted-token auth. |
| **Library** | Recursive scanner reading tags from MP3, FLAC, OGG/Opus, M4A/AAC, WAV, WMA, AIFF, APE, MPC and WavPack. Embedded and folder cover art, MusicBrainz IDs, disc/track numbers, multi-value artist and genre fields. Optional live filesystem watching. |
| **Smart playlists** | Navidrome-compatible rule documents (`all`/`any`/`not` with 19 operators) refreshed on a schedule, **plus** AI-curated playlists that reason over your play history, Last.fm and MusicBrainz data. |
| **AI analytics** | Narrative reports over your listening — taste profile, discovery rate, listening clock, artist affinity — written on top of real SQL aggregates, never invented numbers. |
| **Scrobbling** | Last.fm and ListenBrainz, with a durable retry queue so nothing is lost while a service is down. |
| **Multiuser** | Local accounts, admin-managed. Per-user play counts, stars, ratings, playlists, play queues and scrobble credentials. |
| **Transcoding** | On-the-fly ffmpeg transcoding with per-user bitrate caps, HTTP range support and an LRU disk cache. |
| **Podcasts** | RSS subscriptions, scheduled refresh, episode download and playback through both the web UI and the Subsonic podcast endpoints. |
| **Lidarr** | Two-way sync with your existing Lidarr over its API — push wanted artists/albums, pull imported releases back into the library. Nothing bundled. |
| **Acquisition** | yt-dlp fetching of recommended tracks, behind an approval queue by default, or fully automatic if you choose. |
| **Web UI** | React + Tailwind, dark-first, keyboard shortcuts, queue player, search, discovery, analytics and an admin console. |

---

## Quick start

```bash
git clone https://github.com/dean1850/musicdrome.git
cd musicdrome
cp .env.example .env
```

Edit `.env`. At minimum, point it at your music and set the two secrets:

```bash
MUSIC_DIR=/path/to/your/music
```

```bash
# SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# CREDENTIAL_ENCRYPTION_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Both must be stable values. If `SECRET_KEY` is left blank a fresh one is
generated at every start, which logs everybody out on each restart.

Then:

```bash
docker compose up -d
docker compose logs -f musicdrome
```

That pulls the published image from the GitHub Container Registry — there is
nothing to build. See [Container image](#container-image) if the pull asks you
to authenticate, or if you would rather build from source.

The first start creates an administrator. If you left `DEFAULT_ADMIN_PASSWORD`
blank, a generated one is printed to the log — take it before you lose the
scrollback:

```
  ┌──────────────────────────────────────────────────────────┐
  │  Musicdrome created its first administrator account.     │
  │                                                          │
  │    username: admin                                       │
  │    password: 7f2c9a41e83b5d06                            │
  │                                                          │
  │  Change it after signing in, or set DEFAULT_ADMIN_*      │
  │  in .env before the first start.                         │
  └──────────────────────────────────────────────────────────┘
```

Open <http://localhost:4533> and sign in. The library is scanned on startup;
you can also trigger one from the admin console or with
`docker compose exec musicdrome musicdrome scan`.

The stack is one service and nothing else. Everything optional connects over an
API to something you already run:

| | |
|---|---|
| **Lidarr** | `LIDARR_URL` + `LIDARR_API_KEY` — see [Acquisition and Lidarr](#acquisition-and-lidarr) |
| **Ollama** | `AI_PROVIDER=ollama` + `OLLAMA_BASE_URL` — AI with no API key and nothing leaving your network |
| **Anthropic / OpenAI** | `AI_PROVIDER` + the matching key |
| **Last.fm / ListenBrainz / MusicBrainz** | keys and per-user tokens in Settings |

Anything on the Docker host is reachable at `host.docker.internal` — compose
maps it to the host gateway, so it works on Linux as well as Docker Desktop.
`localhost` will not work; inside the container that is the container itself.

---

## Container image

Images are built by GitHub Actions and published to the GitHub Container
Registry:

```
ghcr.io/dean1850/musicdrome
```

Built for `linux/amd64` and `linux/arm64`, so the same tag runs on x86 servers,
Apple Silicon and a Raspberry Pi 4/5.

| Tag | Points at |
|---|---|
| `latest` | The most recent commit on `main` |
| `v1.2.3`, `1.2`, `1` | A released version — `1.2` and `1` move forward within their range |
| `sha-<commit>` | One exact commit, never reused |
| `<branch>` | The head of that branch, for testing an unmerged change |

Pick which one compose runs with `MUSICDROME_IMAGE` / `MUSICDROME_TAG` in
`.env`. `latest` follows `main`; pin to a `v*` or `sha-*` tag if you would
rather upgrade deliberately.

**Authentication.** The package inherits the repository's visibility. While the
repository is private you need to log in once before pulling — a [personal
access token](https://github.com/settings/tokens) with the `read:packages`
scope is enough:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin
```

To drop that step, open the package page → **Package settings** → **Change
visibility** → **Public**. Nothing in the image contains secrets; your `.env`
stays on the host.

**Updating.**

```bash
docker compose pull
docker compose up -d
```

**Building it yourself.** The build override swaps the published image for a
local build of the working tree:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Or without compose:

```bash
docker build -t ghcr.io/dean1850/musicdrome:latest .
```

**How the publish works.** `.github/workflows/docker-publish.yml` builds on
every push to `main`, on `v*.*.*` tags, and on demand from the Actions tab.
Pull requests build too, `amd64` only and never pushed, so a broken Dockerfile
is caught before it merges. Authentication uses the automatic `GITHUB_TOKEN` —
there are no secrets to configure.

To cut a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

---

## Connecting a Subsonic client

Point any Subsonic client at the same host and port:

| Field | Value |
|---|---|
| Server / URL | `http://your-host:4533` |
| Username | your Musicdrome username |
| Password | your Musicdrome password |

Leave the path empty — clients append `/rest` themselves. Both auth styles work:
legacy `p=` (plaintext or `enc:` hex) and the salted `t=`/`s=` token. Set
`SUBSONIC_REQUIRE_TOKEN_AUTH=true` to reject clients that send the password in
cleartext.

> **Why the server keeps a reversible copy of your password**
>
> Subsonic token auth is `md5(password + salt)`, and the *server* has to compute
> it — which means the server must be able to recover the password. A one-way
> hash cannot satisfy that. Musicdrome therefore stores two copies: an
> **argon2id** hash used for web sign-in, and a **Fernet-encrypted** copy used
> only to answer Subsonic token auth. The encrypted copy is worthless without
> `CREDENTIAL_ENCRYPTION_KEY`, which is why that key is required and why it
> should not live in the same backup as the database.
>
> This is the same tradeoff Navidrome and Airsonic make. It is inherent to the
> protocol, not a shortcut.

---

## Configuration

Every setting is an environment variable read from `.env`. Nothing is configured
in code, and nothing needs a rebuild to change. `.env.example` is the complete
annotated reference; the tables below group the same variables.

<details>
<summary><b>Core server</b></summary>

| Variable | Default | Description |
|---|---|---|
| `MUSICDROME_PORT` | `4533` | HTTP port, used for both the container port and the published one |
| `MUSICDROME_HOST` | `0.0.0.0` | Bind address |
| `MUSICDROME_LOG_LEVEL` | `info` | `debug`/`info`/`warning`/`error` |
| `MUSICDROME_BASE_URL` | – | Set only when serving under a sub-path behind a reverse proxy |
| `MUSICDROME_SERVER_NAME` | `Musicdrome` | Name reported to Subsonic clients |
| `MUSICDROME_CORS_ORIGINS` | `*` | Comma-separated browser origins |
| `MUSICDROME_WORKERS` | `1` | Keep at 1 — SQLite and the scheduler are in-process |
| `TZ` | `UTC` | Container timezone |
| `PUID` / `PGID` | `1000` | User/group the process drops to, so written files match host ownership |

</details>

<details>
<summary><b>Paths</b></summary>

| Variable | Default | Description |
|---|---|---|
| `MUSIC_DIR` | `./data/music` | Your library (mounted at `/music`) |
| `MUSIC_READ_ONLY` | `false` | Stops Musicdrome writing into the library; acquisition imports refuse to run |
| `MUSIC_MOUNT_MODE` | `rw` | Bind-mount mode compose uses. Set to `ro` alongside `MUSIC_READ_ONLY=true` for kernel-level enforcement |
| `DATA_DIR` | `./data/config` | Database, cover-art cache, artist images, logs |
| `CACHE_DIR` | `./data/cache` | Transcode cache — safe to delete at any time |
| `PODCAST_DIR` | `./data/podcasts` | Downloaded episodes |
| `DOWNLOAD_DIR` | `./data/downloads` | yt-dlp staging area before import |

</details>

<details>
<summary><b>Security &amp; accounts</b></summary>

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | *generated* | **Set this.** Signs JWTs; an ephemeral key logs everyone out on restart |
| `CREDENTIAL_ENCRYPTION_KEY` | *generated* | **Set this.** Fernet key for stored third-party credentials and the Subsonic password copy |
| `JWT_ALGORITHM` | `HS256` | |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | |
| `ALLOW_OPEN_REGISTRATION` | `false` | Self-service signup. Off by default — admins create accounts |
| `DEFAULT_ADMIN_USERNAME` | `admin` | Bootstrap admin, created on first start only |
| `DEFAULT_ADMIN_PASSWORD` | *generated* | Leave blank and one is generated and logged |
| `SUBSONIC_REQUIRE_TOKEN_AUTH` | `false` | Reject Subsonic clients sending cleartext passwords |

</details>

<details>
<summary><b>Library scanner</b></summary>

| Variable | Default | Description |
|---|---|---|
| `SCAN_ON_STARTUP` | `true` | Scan when the server boots |
| `SCAN_INTERVAL_MINUTES` | `60` | Minutes between scheduled scans |
| `SCAN_WATCH_FILESYSTEM` | `true` | Watch the library and pick up changes live (debounced) |
| `SCAN_EXTENSIONS` | `mp3,flac,ogg,oga,opus,m4a,m4b,aac,wav,wma,aiff,aif,ape,mpc,wv` | Extensions to index |
| `SCAN_IGNORE_PATTERNS` | `@eaDir,.AppleDouble,#recycle,.stfolder,lost+found` | Path fragments to skip |
| `COVER_ART_NAMES` | `cover,folder,front,album,albumart,thumb` | Folder image basenames, searched in order |
| `MULTIVALUE_SEPARATORS` | `;,/,feat.,ft.` | Separators for multi-value artist/genre tags |
| `ALBUM_GROUPING` | `musicbrainz` | Group by MusicBrainz release ID when present, else artist+album |

</details>

<details>
<summary><b>Transcoding</b></summary>

| Variable | Default | Description |
|---|---|---|
| `TRANSCODING_ENABLED` | `true` | Master switch. If ffmpeg is missing the server logs one warning and serves originals rather than failing |
| `FFMPEG_PATH` | `/usr/bin/ffmpeg` | |
| `FFPROBE_PATH` | `/usr/bin/ffprobe` | |
| `DEFAULT_TRANSCODE_FORMAT` | `mp3` | Used when a client asks to transcode without naming a format |
| `DEFAULT_MAX_BITRATE` | `320` | Ceiling in kbps applied to every stream; `0` = unlimited |
| `TRANSCODE_CACHE_ENABLED` | `true` | Cache finished transcodes so repeat plays are free |
| `TRANSCODE_CACHE_SIZE_MB` | `2048` | Cache budget; oldest entries evicted first |
| `TRANSCODE_MAX_CONCURRENT` | `4` | Concurrent ffmpeg processes |

</details>

<details>
<summary><b>Last.fm</b></summary>

| Variable | Default | Description |
|---|---|---|
| `LASTFM_ENABLED` | `true` | |
| `LASTFM_API_KEY` / `LASTFM_API_SECRET` | – | From <https://www.last.fm/api/account/create> |
| `LASTFM_SCROBBLE_ENABLED` | `true` | Each user links their own account under Settings → Scrobbling |
| `LASTFM_FETCH_SIMILAR` | `true` | Pull similar-artist/track data to feed recommendations |
| `LASTFM_LANGUAGE` | `en` | |

</details>

<details>
<summary><b>ListenBrainz</b></summary>

| Variable | Default | Description |
|---|---|---|
| `LISTENBRAINZ_ENABLED` | `true` | |
| `LISTENBRAINZ_API_URL` | `https://api.listenbrainz.org` | Point at your own instance if you run one |
| `LISTENBRAINZ_SCROBBLE_ENABLED` | `true` | |
| `LISTENBRAINZ_TOKEN` | – | Optional server-wide default; per-user tokens are set in the web UI |

</details>

<details>
<summary><b>MusicBrainz</b></summary>

| Variable | Default | Description |
|---|---|---|
| `MUSICBRAINZ_ENABLED` | `true` | Metadata enrichment |
| `MUSICBRAINZ_API_URL` | `https://musicbrainz.org/ws/2` | |
| `MUSICBRAINZ_RATE_LIMIT` | `1.0` | Seconds between requests. Do not lower this against the public server |
| `MUSICBRAINZ_USER_AGENT` | `Musicdrome/1.0.0 (…)` | Required by MusicBrainz; put your own contact URL here |
| `MUSICBRAINZ_ENRICH_MODE` | `all` | `all` = look everything up; otherwise only tracks that already carry an MBID |

</details>

<details>
<summary><b>AI</b></summary>

| Variable | Default | Description |
|---|---|---|
| `AI_ENABLED` | `true` | Master switch for curation and analytics. Inert until a provider is configured |
| `AI_PROVIDER` | `anthropic` | `anthropic`, `ollama`, or `openai` (any OpenAI-compatible endpoint) |
| `ANTHROPIC_API_KEY` | – | Required for the `anthropic` provider |
| `ANTHROPIC_MODEL` | `claude-opus-5` | |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | |
| `ANTHROPIC_EFFORT` | `medium` | Thinking depth: `low`/`medium`/`high`/`xhigh`/`max` |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Your own Ollama, as reachable from inside the container |
| `OLLAMA_MODEL` | `llama3.1` | |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | `https://api.openai.com/v1`, –, `gpt-4o-mini` | For OpenAI, LM Studio, vLLM, OpenRouter … |
| `AI_MAX_TOKENS` | `8192` | Response budget |
| `AI_TEMPERATURE` | `0.7` | **ollama/openai only** — Claude models reject sampling parameters, so nothing is sent to Anthropic |
| `AI_REQUEST_TIMEOUT` | `180` | Seconds |
| `AI_PLAYLIST_REFRESH_HOURS` | `24` | How often AI playlists are regenerated |
| `AI_ANALYTICS_REFRESH_HOURS` | `24` | How often the analytics report is rebuilt |
| `AI_MIN_PLAYS_FOR_PROFILE` | `20` | Skip profiling users with less history than this |
| `AI_CONTEXT_TRACK_LIMIT` | `300` | Candidate tracks handed to the model when curating |

</details>

<details>
<summary><b>Smart playlists</b></summary>

| Variable | Default | Description |
|---|---|---|
| `SMART_PLAYLIST_ENABLED` | `true` | |
| `SMART_PLAYLIST_REFRESH_MINUTES` | `60` | |
| `SMART_PLAYLIST_MAX_TRACKS` | `100` | Default cap on generated length |
| `SMART_PLAYLIST_SEED_DEFAULTS` | `true` | Create the starter set for each new user |

</details>

<details>
<summary><b>Podcasts</b></summary>

| Variable | Default | Description |
|---|---|---|
| `PODCAST_ENABLED` | `true` | |
| `PODCAST_REFRESH_HOURS` | `6` | Feed poll interval |
| `PODCAST_AUTO_DOWNLOAD` | `false` | Download new episodes instead of streaming on demand |
| `PODCAST_KEEP_EPISODES` | `10` | Retained downloads per channel; `0` = keep all |
| `PODCAST_MAX_CONCURRENT_DOWNLOADS` | `2` | |

</details>

<details>
<summary><b>Lidarr</b></summary>

| Variable | Default | Description |
|---|---|---|
| `LIDARR_ENABLED` | `false` | |
| `LIDARR_URL` | `http://host.docker.internal:8686` | Your Lidarr, as reachable *from inside the container* — see below |
| `LIDARR_API_KEY` | – | Lidarr → Settings → General |
| `LIDARR_ROOT_FOLDER` | `/music` | The root folder *as Lidarr sees it*, which is probably not `/music` if Lidarr runs elsewhere |
| `LIDARR_QUALITY_PROFILE_ID` | `1` | |
| `LIDARR_METADATA_PROFILE_ID` | `1` | |
| `LIDARR_MONITOR_MODE` | `all` | `all`/`future`/`missing`/`existing`/`latest`/`first`/`none` |
| `LIDARR_SYNC_INTERVAL_MINUTES` | `30` | |
| `LIDARR_PUSH_WANTED` | `true` | Push wanted artists/albums into Lidarr |
| `LIDARR_PULL_IMPORTED` | `true` | Poll for imported releases and rescan those paths |
| `LIDARR_SEARCH_ON_ADD` | `true` | Ask Lidarr to search indexers immediately on push |

</details>

<details>
<summary><b>Acquisition (yt-dlp)</b></summary>

| Variable | Default | Description |
|---|---|---|
| `ACQUISITION_ENABLED` | `true` | |
| `AUTO_DOWNLOAD` | `false` | `false` = recommendations queue as **Wanted** and wait for approval. `true` = anything above `ACQUISITION_MIN_CONFIDENCE` downloads unattended |
| `ACQUISITION_MIN_CONFIDENCE` | `0.7` | Confidence gate for automatic downloads |
| `ACQUISITION_MAX_PER_DAY` | `25` | Safety valve on unattended downloads |
| `ACQUISITION_MAX_CONCURRENT` | `2` | |
| `ACQUISITION_SEARCH_PREFIX` | `ytsearch5` | yt-dlp search expression |
| `ACQUISITION_IMPORT_TEMPLATE` | `{artist}/{album}/{track:02d} - {title}.{ext}` | Layout under `MUSIC_DIR` |
| `YTDLP_FORMAT` | `bestaudio/best` | |
| `YTDLP_AUDIO_FORMAT` | `mp3` | Extracted codec |
| `YTDLP_AUDIO_QUALITY` | `0` | 0 = best, 9 = worst |
| `YTDLP_COOKIES_FILE` | – | Netscape-format cookies, path inside the container |
| `YTDLP_PROXY` / `YTDLP_RATE_LIMIT` | – | |

</details>

<details>
<summary><b>Recommendations</b></summary>

| Variable | Default | Description |
|---|---|---|
| `RECOMMENDATIONS_ENABLED` | `true` | |
| `RECOMMENDATION_REFRESH_HOURS` | `12` | |
| `RECOMMENDATION_SOURCES` | `lastfm,listenbrainz,ai` | Any of `lastfm`, `listenbrainz`, `ai` |
| `RECOMMENDATION_LIMIT` | `50` | Items kept per user |

</details>

---

## Architecture

```
                        ┌──────────────────────────────┐
   Subsonic clients ───▶│  /rest    Subsonic API       │
   (DSub, Symfonium…)   │           v1.16.1 + OpenSub  │
                        ├──────────────────────────────┤
   Browser ────────────▶│  /        React SPA (dark)   │
                        │  /api/v1  Native JSON API    │
                        ├──────────────────────────────┤
                        │  FastAPI  ·  SQLAlchemy      │
                        │  APScheduler (in-process)    │
                        └───────┬──────────────────────┘
                                │
        ┌───────────┬───────────┼───────────┬─────────────┐
        ▼           ▼           ▼           ▼             ▼
    ┌───────┐  ┌─────────┐  ┌────────┐  ┌────────┐  ┌───────────┐
    │Scanner│  │Transcode│  │Scrobble│  │ Smart  │  │Acquisition│
    │mutagen│  │ ffmpeg  │  │ queue  │  │playlist│  │  yt-dlp   │
    │watchdog│ │  cache  │  │ retry  │  │ engine │  │ + Lidarr  │
    └───┬───┘  └─────────┘  └───┬────┘  └───┬────┘  └─────┬─────┘
        │                       │           │             │
        ▼                       ▼           ▼             ▼
    ┌────────────┐      ┌─────────────────────────────────────┐
    │  SQLite    │      │ Last.fm · ListenBrainz · MusicBrainz │
    │  (WAL)     │      │ Anthropic / Ollama / OpenAI · Lidarr │
    └────────────┘      └─────────────────────────────────────┘
```

One process, one container, one database file. No Redis, no Celery, no separate
worker — background jobs run on APScheduler inside the app, which is the right
size for a personal music server.

---

## Smart playlists

Rule documents follow Navidrome's format. A rule is a JSON object with boolean
combinators and optional `sort`/`order`/`limit`/`offset` keys:

```json
{
  "all": [
    { "any": [
        { "is": { "genre": "Jazz" } },
        { "is": { "genre": "Blues" } }
      ]
    },
    { "gt": { "playCount": 2 } },
    { "inTheLast": { "lastPlayed": 90 } },
    { "is": { "starred": true } }
  ],
  "sort":  "lastPlayed",
  "order": "desc",
  "limit": 100
}
```

**Track fields** — `title`, `album`, `artist`, `albumArtist`, `genre`, `year`,
`trackNumber`, `discNumber`, `bitRate`, `duration`, `size`, `comment`, `bpm`,
`filePath`, `fileType`, `dateAdded`, `dateModified`.

**Per-user fields** — `playCount`, `lastPlayed`, `rating`, `starred`, `loved`.

Field names are matched case-insensitively and ignore underscores, so
`albumArtist`, `albumartist` and `album_artist` are the same field.

**Operators** — `is`/`eq`, `isNot`/`ne`, `gt`, `gte`, `lt`, `lte`, `contains`,
`notContains`, `startsWith`, `endsWith`, `inTheRange` (`[min, max]`), `before`,
`after`, `inTheLast` (days), `notInTheLast` (days), `isNull`, `isNotNull`, plus
the combinators `all`, `any`, `not`.

`inTheLast` and `notInTheLast` require a date field (`dateAdded`,
`dateModified`, `lastPlayed`). `sort` accepts any field plus `random`.

Six starter playlists are created for each new account — Recently Added, Most
Played, Forgotten Gems, Never Played, Favourites, Top Rated — and are populated
immediately rather than waiting for the first scheduled refresh. Set
`SMART_PLAYLIST_SEED_DEFAULTS=false` to skip them.

---

## AI features

With `AI_ENABLED=true` and a provider configured, three things become available.

**AI-curated playlists.** The curator assembles a candidate pool from your
library, play history, Last.fm similar-artist data and MusicBrainz relations,
then hands the model a *numbered list of tracks it may choose from*. Any ID the
model returns that is not in that list is discarded. The model picks and
sequences; it cannot invent a track you do not own.

**Analytics.** Every number in a report — play counts, unique artists, discovery
rate, hour-of-day distribution — comes from SQL. The model writes the narrative
around those figures. With AI disabled you still get the whole dashboard, just
without the prose.

**Recommendations.** Seeded from Last.fm and ListenBrainz, ranked against your
history, feeding the acquisition queue.

The provider is pluggable, and none of them are bundled — you point Musicdrome
at one:

```bash
AI_PROVIDER=anthropic     # ANTHROPIC_API_KEY
AI_PROVIDER=ollama        # OLLAMA_BASE_URL — your own instance, no API key,
                          # nothing leaves your network
AI_PROVIDER=openai        # OPENAI_BASE_URL + OPENAI_API_KEY, or any
                          # OpenAI-compatible endpoint (LM Studio, vLLM, …)
```

---

## Acquisition and Lidarr

```
  Recommendation ──▶ Wanted item ──▶ [ approval queue ] ──▶ Fetch ──▶ Import
   (Last.fm/LB/AI)                          │                 │          │
                                            │           yt-dlp or     scanner
                                  AUTO_DOWNLOAD=true       Lidarr    picks it up
                                  skips this gate
```

With `AUTO_DOWNLOAD=false` (the default) nothing is downloaded until you approve
it under **Discover → Wanted**. Set it to `true` and anything scoring above
`ACQUISITION_MIN_CONFIDENCE` is fetched unattended, capped by
`ACQUISITION_MAX_PER_DAY`.

### Connecting Lidarr

Musicdrome talks to a Lidarr you already run — it does not ship one. Set three
values in `.env`:

```bash
LIDARR_ENABLED=true
LIDARR_URL=http://host.docker.internal:8686
LIDARR_API_KEY=...                 # Lidarr → Settings → General → API Key
LIDARR_ROOT_FOLDER=/music          # the library path as *Lidarr* sees it
```

`LIDARR_URL` must be reachable **from inside the Musicdrome container**, which
is the one thing that trips people up:

| Where Lidarr runs | Use |
|---|---|
| On the Docker host | `http://host.docker.internal:8686` — compose maps this to the host gateway, so it works on Linux too, not just Docker Desktop |
| Elsewhere on your LAN or a NAS | `http://192.168.1.20:8686` |
| In a different compose stack | Attach Musicdrome to that stack's network, then use the service name: `http://lidarr:8686` |
| Behind a reverse proxy | The external URL, e.g. `https://lidarr.example.com` |

`http://localhost:8686` will **not** work — inside the container that is the
container itself.

`LIDARR_ROOT_FOLDER` is Lidarr's own path for the library, not Musicdrome's. If
Musicdrome sees `/music` but Lidarr calls the same folder `/data/media/music`,
use the latter. Both do need to be pointed at the same actual directory for
imports to land somewhere Musicdrome will scan.

Sync is two-way: approved wanted items are pushed as monitored artists/albums,
and releases Lidarr finishes importing are pulled back and indexed.

> Musicdrome downloads what you tell it to download. You are responsible for
> having the right to obtain the material you queue, and for whatever your Lidarr
> indexers are configured to reach. The approval queue is the default for exactly
> this reason.

---

## Running from source

```bash
# Backend
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m musicdrome.cli scan
uvicorn musicdrome.main:app --reload --port 4533
```

```bash
# Frontend, in a second shell
cd frontend
npm install
npm run dev          # proxies /api and /rest to :4533
```

For a production-shaped run, `npm run build` and set
`MUSICDROME_STATIC_DIR=/path/to/frontend/dist` — the backend then serves the SPA
itself, exactly as it does in the container.

### CLI

```bash
docker compose exec musicdrome musicdrome <command>
```

| Command | Purpose |
|---|---|
| `scan [--full]` | Index the library; `--full` re-reads tags for unchanged files |
| `create-user NAME [--password P] [--admin]` | Add an account; prompts if no password given |
| `set-password NAME [--password P]` | Reset a password (writes both stored copies) |
| `list-users` | Show accounts and roles |
| `config` | Print the effective configuration with secrets masked |
| `refresh {playlists,recommendations,podcasts,metadata}` | Run a maintenance task now |

---

## Testing

Playwright drives both suites — the browser UI and the Subsonic API at the
request level.

```bash
cd frontend && npm install && npm run build    # the E2E specs need a built UI
cd ../tests && npm install && npm test
```

The runner starts a throwaway server on port 4599, synthesises a 19-track
library from scratch (no audio binaries in the repo), scans it, and runs 106
specs covering auth and multiuser separation, browsing and search, real audio
playback, playlists, discovery, podcasts, admin, dark mode, and Subsonic
conformance — both auth mechanisms, XML/JSON/JSONP envelopes and the Subsonic
error codes.

To test a server that is already running — your compose stack, a dev server —
point the suite at it instead:

```bash
E2E_BASE_URL=http://localhost:4533 npm test
```

That path writes to the target (accounts, playlists, play counts) and the
browsing specs assert counts from the seeded library, so aim it at a throwaway
instance rather than your real one. See [`tests/README.md`](tests/README.md).

---

## Troubleshooting

**Everyone is logged out after a restart.** `SECRET_KEY` is blank, so a new one
is generated each boot. Set it in `.env`.

**Subsonic clients cannot log in but the web UI works.** The account predates
`CREDENTIAL_ENCRYPTION_KEY` being set, or the key changed. Reset the password
with `musicdrome set-password NAME` — that writes both stored copies.

**The scan finds nothing.** Check the library is readable inside the container
(`docker compose exec musicdrome ls /music`) and that your files' extensions are
listed in `SCAN_EXTENSIONS`.

**Playback works but transcoding does not.** If ffmpeg is unavailable the server
logs one warning and serves original files rather than erroring. Verify with
`docker compose exec musicdrome ffmpeg -version` and check `FFMPEG_PATH`.

**Scrobbles are not appearing.** They queue and retry. A per-user Last.fm link
or ListenBrainz token is required in **Settings → Scrobbling** in addition to
the server-level API key.

**Files land with the wrong owner.** Set `PUID`/`PGID` to match your host user.

**Acquisition imports fail.** Check `MUSIC_READ_ONLY` is `false` and, if you set
`MUSIC_MOUNT_MODE=ro`, that the mount is writable.

---

## Project layout

```
backend/musicdrome/
  main.py            app, lifespan, admin bootstrap, SPA serving
  config.py          every setting, read from .env
  models.py          SQLAlchemy models
  security.py        argon2id, Fernet, Subsonic token, JWT
  subsonic/          /rest — system, browsing, media, playlists
  api/               /api/v1 — auth, library, playlists, discovery,
                     podcasts, admin
  services/          scanner, tags, watcher, transcode, scrobble,
                     lastfm, listenbrainz, musicbrainz, smartplaylist,
                     podcasts, lidarr, acquisition, recommendations, jobs
  services/ai/       provider abstraction, curator, analytics
frontend/src/        React SPA — pages, components, stores
tests/               Playwright E2E + Subsonic conformance
docker/              entrypoint
.github/workflows/   image build and publish to ghcr.io
```

---

## Credits

Built as a drop-in alternative to [Navidrome](https://github.com/navidrome/navidrome),
whose Subsonic behaviour and smart-playlist rule format it deliberately matches.
Lidarr integration targets [Lidarr](https://github.com/lidarr/lidarr). Track
acquisition uses [yt-dlp](https://github.com/yt-dlp/yt-dlp), in the spirit of
[Downtify](https://github.com/henriquesebastiao/downtify). Metadata from
[MusicBrainz](https://musicbrainz.org) and [Last.fm](https://www.last.fm);
listening history to [ListenBrainz](https://listenbrainz.org).

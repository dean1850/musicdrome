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

### Building from source instead

If you have the repository checked out — to run an unpushed change, or because
the published image isn't reachable — layer the build override on top:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

> **If you fork this and pulling gives `unauthorized`:** GHCR packages are
> private by default even when the repository is public, so a successful build
> doesn't publish the package. Set it to public under your profile →
> **Packages** → `musicdrome` → **Package settings** → **Change visibility**,
> or build from source with the command above.

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
| Listeners | One | Who lives here, and their scrobble accounts |
| Schedule | Daily | Off, every 6 hours, daily or weekly |
| Tracks per scan | 40 | 5–100, one AI call regardless |
| Listening window | 90 days | How far back the taste profile reaches |
| Auto-download | Off | Download anything above a match threshold |
| Threshold / daily cap | 85% / 25 | The cap counts finished downloads in 24 hours |
| Hide below | 0% | Display filter for the card grid |
| Retention | 60 days | Un-actioned cards are purged; decisions are kept |
| Taste summary | On | One AI call a day for the stats page |

## Listeners

A household is rarely one set of ears. Musicdrome keeps a list of **listeners**,
each with their own Last.fm and ListenBrainz accounts, and everything that is a
matter of taste is kept per person: their scrobble history, their recommendation
cards, their saved and hidden decisions, their stats page.

What is shared is the music. One library on disk, one download queue — because a
house has one record collection. Hiding a card never removes it from someone
else's grid, and the same track can be recommended to two people independently.

There are no passwords. A listener is a taste profile, not an account, and the
picker in the header works the way a streaming box asks who is watching. Put
Musicdrome behind a VPN or an authenticating proxy if it needs to be reachable
from outside — see [Security](#security).

Add people in **Settings → Listeners**. Scans run for whoever is selected;
scheduled scans run for every active listener in turn, so everyone's grid is
fresh in the morning. Deactivate someone to keep their history but skip them.

### Importing from Navidrome

If you already run [Navidrome](https://github.com/navidrome/navidrome), it knows
who lives here. Set the admin credentials in `.env`:

```bash
NAVIDROME_URL=http://192.168.1.5:4533
NAVIDROME_USER=admin
NAVIDROME_PASSWORD=...
```

and **Import from Navidrome** appears in Settings. It brings across the account
names and mail addresses. Nothing is ever written back.

**It cannot import anyone's Last.fm account, and no integration could.**
Navidrome's Subsonic `getUsers` returns usernames, mail addresses and role flags
only; each user's linked scrobble session is kept private and is never exposed
over the API, not even to an admin. So the roster arrives automatically and the
scrobble usernames are filled in once per person, by hand, in Settings.

Listing users is admin-only, which is why the credentials above have to be an
admin's. The password is never sent — Subsonic's salted-token scheme is used.

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

## yt-dlp versions

Will restarts pick up new yt-dlp versions? New stable releases, yes — they hit
PyPI the moment they're tagged on GitHub, and your container upgrades on every
boot (see `YTDLP_AUTO_UPDATE` in `.env.example`). Nightlies, no — pip skips
pre-releases without `--pre`.

### When YouTube breaks and stable is behind

Stable is currently five weeks old while nightlies are eight days old, so this
will come up. Escape hatch, no image rebuild:

```bash
docker compose exec musicdrome pip install --no-cache-dir --pre --upgrade yt-dlp
docker compose restart musicdrome
```

## When downloads fail

Two failures account for almost all of it, and neither says what it means.

### "Permission denied: /music/&lt;Artist&gt;"

The library is not writable by the container. This is the nastiest one to
diagnose unaided, because everything else works perfectly: the app boots, scans
run, matches are found, yt-dlp downloads the audio and ffmpeg encodes it — and
then the very last step, creating the artist folder, fails. The bandwidth is
already spent.

Musicdrome now checks this at boot and again before each download, and says
which uid it is running as and which owns the directory. The container runs as
**root by default** precisely so this does not happen; if you have set
`PUID`/`PGID` to a normal user, make sure that user can write to `MUSIC_DIR`:

| Mount | Fix |
|---|---|
| Local disk, ZFS, unRAID | `sudo chown -R 1000:1000 /path/to/music` |
| CIFS/SMB | Mount with `uid=1000,gid=1000,file_mode=0664,dir_mode=0775` — `chown` does not work on CIFS |
| NFS | Match the uid on the server, or map it with `anonuid` |

**Read the errno before you chown anything.** The message ends in the reason
the write failed, and only `Permission denied` / `Read-only file system` are
about permissions at all. `No such file or directory` or `Stale file handle`
means the mount behind `MUSIC_DIR` has gone away underneath the container,
which no amount of `chown` will fix — check the share is still connected.
Musicdrome words those two cases differently for that reason, and only the
permissions one mentions `PUID`/`PGID`.

One tell is worth knowing: if the message names the **same uid** for the
process and the directory — "Running as 0:0, directory is owned by 0:0" — it
was never a permissions problem. Versions before this fix could report that
spuriously when two downloads were requeued at the same moment, because the
concurrent write probes raced to clean up after each other. Fixed; if you see
it on an older image, the mount is fine and the download is worth retrying.

### "formats have been skipped as they are missing a URL"

This is [yt-dlp#12482](https://github.com/yt-dlp/yt-dlp/issues/12482) — YouTube
moving a client onto SABR, where the player response carries no format URLs at
all. It is a **warning, not a failure**: yt-dlp says it while falling through to
a client that does work, and it is filtered out of the log.

It becomes a real failure only if every client is SABR-only, which is what
happens when the client list is pinned to a stale set. So don't pin it —
`YTDLP_PLAYER_CLIENTS` is empty by default and yt-dlp's own defaults track
YouTube as it changes. The clients that still serve real URLs need a JavaScript
runtime to solve YouTube's signature challenges; the image ships Deno for that,
plus yt-dlp's solver scripts (`YTDLP_JS_RUNTIMES`, `YTDLP_REMOTE_COMPONENTS`).

### "No supported JavaScript runtime could be found", or HTTP 403

The same root cause, one step earlier. yt-dlp needs an external JavaScript
runtime to answer YouTube's challenges, and when it cannot find a usable one it
does not stop — it falls back to clients that need no JavaScript, and YouTube
answers those with `HTTP Error 403: Forbidden` partway through the download.

**A runtime that is installed but too old counts as missing.** yt-dlp requires
Deno ≥ 2.3, Node ≥ 22, or QuickJS ≥ 2023-12-9, and anything below the floor is
ignored exactly as if it were absent. Images built before this fix installed
Debian's `nodejs` — 18 on bookworm, 20 on trixie — so the runtime was present,
rejected and never once used. The image now ships Deno, which is yt-dlp's own
recommended runtime, and Musicdrome logs which runtime it found at boot:

```
INFO  app.download: javascript: deno 2.9.5
```

If that line is missing, or an error follows it, downloads will 403. Pull the
current image; `YTDLP_JS_RUNTIMES` only needs setting if you are running
outside the image and want to name a runtime of your own.

### "no confident match on YouTube Music or YouTube"

Not a download failure — nothing was downloaded because nothing credible was
found. Musicdrome refuses to file a track it cannot attribute to the right
artist, on the grounds that a tribute band in your library is worse than a gap.
If it happens constantly, the AI is probably recommending obscure or misspelled
titles; a narrower listening window tends to help.

## Security

Musicdrome has **no authentication**, by design — it is meant for a trusted home
network. Listeners are taste profiles, not accounts: picking a different one is
not a login and is not a security boundary. Put it behind a VPN or an
authenticating reverse proxy if you need to reach it from outside.

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

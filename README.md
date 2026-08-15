# Musicdrome

AI music discovery that downloads what it recommends.

Musicdrome reads what you already listen to from Last.fm and ListenBrainz —
and, optionally, what you have *hearted* in Navidrome — asks an AI what you
would like next, scores every suggestion with a match percentage, and downloads
the ones you pick as tagged MP3s at 320 kbps.

Single container: FastAPI plus vanilla HTML, CSS and JavaScript. No database
server, no build step, no telemetry, no third-party embeds. Dark only.

```
   your scrobbles          one AI call            you decide
 Last.fm ─┐                     │                      │
          ├──▶ SQLite ──▶ 40 ranked tracks ──▶  ↓ download  ♥ save  ✕ hide
 ListenBrainz ┘           + match % + why            │
 Navidrome ♥ ┘                                       ▼
                              YouTube Music ─▶ yt-dlp ─▶ Opus ~160
                                                     │
                                MusicBrainz tags, cover art, and
                                Artist/Album/01 - Title.opus
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

The shipped `docker-compose.yml` routes Musicdrome through
[gluetun](https://github.com/qdm12/gluetun), so downloads leave over a VPN. Port
3046 is then published by the gluetun service rather than this one. Not using
gluetun? Drop `network_mode` and `networks` and add a `ports:` block — the file
says exactly what, in a comment at the top.

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

**Optionally, Navidrome.** Your hearted tracks, which are a much better signal
than your plays — see [What you hearted](#what-you-hearted).

**An AI backend.** One of:

| `AI_PROVIDER` | Needs | Notes |
|---|---|---|
| `ollama` | A running Ollama | Local and free. One call per scan means an 8B model is genuinely fine here. |
| `anthropic` | `ANTHROPIC_API_KEY` | |
| `openai` | `OPENAI_API_KEY` | Also works with LM Studio, vLLM or anything OpenAI-compatible via `OPENAI_BASE_URL`. |

## How a scan works

1. **Sync.** New scrobbles are pulled from every configured source. Each keeps
   its own cursor, so only new plays are fetched. Navidrome's hearts are
   re-read at the same time.
2. **Profile.** Your most-played artists, most-played tracks and most recently
   discovered artists over the listening window — plus everything you hearted.
3. **Ask.** One AI call returns the whole batch — 40 tracks by default, each with
   an artist, a title, a match percentage and a one-line reason naming what in
   your history led there.
4. **Resolve.** Each answer goes to MusicBrainz for the canonical artist, album,
   year and recording length, then to Last.fm for cover art and genre tags.
5. **Score.** The match percentage is the model's confidence plus whatever your
   hearts add to it.
6. **Filter.** Anything you already have is dropped — see below.
7. **Show.** What survives becomes cards you can download, save or hide.

Downloads then search YouTube Music through `ytmusicapi`, score candidates on
artist, title and duration against the MusicBrainz recording, and fall back to a
plain YouTube search when YouTube Music does not carry the track. A card that
cannot be matched confidently is marked failed rather than guessed at — a wrong
file in your library is worse than a missing one.

## What you hearted

Scrobbles are a big, noisy signal. A high play count can mean a record you love
or an album that was on in the background for a fortnight, and nothing in the
data tells the two apart. Musicdrome has always had to guess.

A **starred track in Navidrome is not a guess**. Nobody hearts something by
accident, or because it was next in the queue. There are two orders of magnitude
fewer of them than there are scrobbles, and that scarcity is exactly what makes
them worth having. Point Musicdrome at your Navidrome and it reads them:

```sh
NAVIDROME_URL=http://navidrome:4533
NAVIDROME_USER=you
NAVIDROME_PASSWORD=your-navidrome-password
```

Leave `NAVIDROME_URL` empty and nothing changes; everything below is optional.

### There is no Navidrome API key

Worth saying plainly, because it is the first thing you will go looking for.
Navidrome's Subsonic API authenticates a **user** — its
[`validateCredentials`](https://github.com/navidrome/navidrome/blob/master/server/subsonic/middlewares.go)
accepts a plaintext password, a hex-encoded one, an MD5 of the password salted
per request, or a session JWT, and nothing else. The native REST API takes a
JWT that its own documentation calls unstable and asks you not to use. So this
is your Navidrome login, not a token you mint somewhere.

**The password is never sent.** Every request carries an MD5 of it hashed
against fresh random bytes, which is the same scheme every Subsonic client
uses — so it stays out of Navidrome's access log, out of any proxy in front of
it, and out of Musicdrome's own logs. The salt is regenerated per request, not
per session, so one captured request is not a reusable credential.

Musicdrome only ever reads: `ping`, `getStarred2` and `search3`. None of the
three writes anything. Navidrome has no scoped tokens, though, so the
credential is as privileged as the account behind it — if that matters to you,
make an ordinary non-admin Navidrome user for this. Hearts are per-user, so
star from the same account you name here or there will be nothing to read.

### What it changes

**The AI is told.** The prompt gains your hearted artists, hearted tracks and
hearted genres, labelled as the stronger evidence and placed *above* the play
counts — a model reading a long prompt weights the top of it more heavily, and
that is the whole point of the ordering.

**And the score is adjusted afterwards.** Being told is not the same as
listening: one AI call per scan is what makes a local 8B model a reasonable
backend here, and an 8B model handed a new section of the prompt will sometimes
use it and sometimes paraphrase it back at you without letting it move the
number. So the hearts are applied twice — once in the prompt, and again in
arithmetic that does not depend on the model having cooperated:

| Signal | Adds |
|---|---|
| The suggested artist is one you have hearted | +12 |
| It came from an artist you have hearted | +8 |
| The artist is among your most played in Navidrome | +6 |
| Its genre is one you heart | +4 |

Capped at **+15 combined**, which is deliberately not enough to carry a bad
recommendation into auto-download territory on its own. The model has heard
your whole listening history; this has heard which artists you starred. It is a
thumb on the scale, and a thumb is the right amount of pressure — a larger
boost would turn every scan into a list of tracks by the six artists you have
hearted, which is the opposite of discovery.

Cards that were lifted say so. The match pill carries a ♥ and hovering it gives
the breakdown — `82% from the model, +12 from what you heart — you have hearted
3 tracks by Boards of Canada` — so the second signal is something you can check
rather than take on trust. Both halves are stored, so the number is never
asserted without being accountable for.

**And hearted tracks are never suggested back to you.** You own them, and you
thought about them enough to star them.

### Play counts

Navidrome also knows how often you have played each track from your own
library, which catches listening that never scrobbled at all. Those are read
too, and they are the one expensive part: Subsonic has no "songs I have played"
endpoint, so they come from paging through the whole library with `search3` —
about 40 requests for 20,000 tracks. That walk is cached for six hours
(`NAVIDROME_LIBRARY_MAX_AGE`) rather than repeated every scan; hearts are one
request and refresh every time. Set `NAVIDROME_LIBRARY_PAGE=0` to skip the walk
entirely and use hearts alone.

They are kept as an aggregate rather than merged into your scrobble history,
and that is deliberate. Navidrome reports *34 plays, most recently Tuesday* —
not the 34 listens behind it. Writing those as play rows would mean inventing
timestamps, which the stats page would then chart as if someone had really
listened at those moments. So the stats tab still counts only real scrobbles;
the taste profile reads the aggregate as the aggregate it is.

Un-hearting in Navidrome takes effect on the next scan: the flag is cleared,
the boost stops, and the track becomes suggestable again.

## What it will never suggest

A track is excluded if any of these is true:

- it is anywhere in your scrobble history
- Musicdrome already downloaded it
- you dismissed it with ✕
- you hearted it in Navidrome
- it was found in `EXCLUDE_MUSIC_DIR`, which is your library

Only *hearted* Navidrome tracks are excluded, not everything Navidrome knows
about — the play-count walk reads your whole library, but suppressing every
recommendation of anything already on the disk is a much larger decision than
it looks. `EXCLUDE_MUSIC_DIR` is the setting for that, and it is opt-in for
exactly that reason.

That last one is the collection you already have. Compose points it at `/music`,
the same directory downloads are filed into, which is the point: everything you
owned before Musicdrome existed is excluded, and so is everything it has fetched
since. Only artist and title tags are read — no library database is built,
nothing is moved, nothing is written that Musicdrome did not download.

Matching is done on normalised keys, so "The Beatles" and "Beatles", or
"Karma Police" and "Karma Police (Remastered 2016)", are recognised as the same
thing rather than suggested back to you.

## Configuration

`.env` carries startup concerns only — keys, usernames, paths, port. Two paths
have no default and describe your machine: `MUSIC_LOCATION`, your library, and
`DATA_LOCATION`, where the SQLite database goes. Inside the container they are
always `/music` and `/config`; `docker-compose.yml` sets that and you never need
to. See [`.env.example`](.env.example).

Your Navidrome credentials live there too, alongside the Last.fm key rather
than in the Settings tab — so no password is ever written to the database or
served back over HTTP. Changing them needs a container restart; the Connections
panel in Settings shows whether they are working.

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

## One listener

Musicdrome recommends for one person. `LASTFM_USER`, `LISTENBRAINZ_USER` and
`NAVIDROME_USER` in `.env` are who that is, and there is nothing else to set
up — no accounts, no profiles, no picker. Navidrome's hearts are per-user, so
that name decides whose hearts are read.

This was briefly a household app, with a users table and per-person suggestions.
It was more machinery than the job needed: one library, one queue and one taste
profile is what actually gets used. Upgrading from a multi-user image drops the
listening history and re-syncs it from Last.fm and ListenBrainz on the next scan
(they hold every scrobble; nothing is lost that cannot be fetched again).
Downloads are kept, because those rows name files that are on your disk. The old
database is copied to `musicdrome.db.pre-single-user` first, in case you want to
go back.

## Downloads

**Opus, copied rather than re-encoded.** YouTube and YouTube Music serve their
best audio as Opus at around 160 kbps, so that is the stream Musicdrome asks
for and ffmpeg copies it into an `.opus` file untouched. What lands on disk is
the source, bit for bit — no second lossy encode, and about half the size of
the MP3 320 this used to produce. Files are tagged from MusicBrainz (artist,
title, album, album artist, year, track number, recording MBID) with cover art
embedded, and filed as:

```
/music/Radiohead/OK Computer/06 - Karma Police.opus
/music/playlist/Musicdrome.m3u
```

Set `AUDIO_FORMAT=mp3` and `AUDIO_BITRATE=320` in `.env` for the old behaviour
if something in the house cannot read Opus — though Navidrome, Plex and
Jellyfin all transcode on the fly for the client that needs it, which is the
better place to pay that cost. Existing MP3s are left where they are; the
change only affects what is downloaded next.

**No lossless is on offer here, from anyone.** YouTube re-encodes every upload
and discards the source, so the artist's master is not retrievable by any tool.
What is achievable is *no second encode*, and that is what copying the Opus
stream gets you. Converting YouTube's audio to FLAC would triple the file size
and recover nothing, which is why `AUDIO_FORMAT=flac` is a supported setting
and a bad idea.

The exception is the minority of tracks YouTube serves only as AAC. Those are
re-encoded to Opus at `AUDIO_BITRATE` (256 by default — comfortably past the
point where a difference has been demonstrated, since re-encoding cannot
recover what the AAC encoder already threw away).

**Which of the two happened is recorded per track.** The Downloads tab has an
Audio column reading `opus 160k copied` or `aac 129k converted`, and the same
phrase goes in the log line for every import. It is measured from the stream
yt-dlp selected against the container that was written, not inferred from the
settings — so "nothing was re-encoded" is something you can check rather than
something you have to take on trust. Downloads from before this shipped show
`—`: a finished file cannot say what it used to be, and a backfilled guess
would be indistinguishable from a measurement.

**One playlist, appended to forever.** Every download lands in
`playlist/Musicdrome.m3u` with relative paths, so the folder can be moved
without breaking it, and a re-download never doubles an entry. Navidrome, Plex
and Jellyfin import it as a single playlist that grows — which is the point:
per-scan playlists produced a wall of `musicdrome-scan-0001`, `-0002`, `-0003`
that nobody opened twice. Old per-scan files are merged into it and deleted the
first time the new image boots. Rename it with `MUSICDROME_PLAYLIST_NAME`.

Jellyfin, Navidrome, Plex and friends read this layout as-is — point them at the
same directory.

### Where the playlist goes

`PLAYLIST_FOLDER` decides, and it is the setting most likely to determine
whether your music server ever imports the thing. With
`MUSIC_LOCATION=/mnt/lan-mount/media/music`:

| `PLAYLIST_FOLDER` | Playlist written to |
|---|---|
| `playlist` *(default)* | `/mnt/lan-mount/media/music/playlist/Musicdrome.m3u` |
| `media/playlists` | `/mnt/lan-mount/media/music/media/playlists/Musicdrome.m3u` |
| `.` | `/mnt/lan-mount/media/music/Musicdrome.m3u` |
| `/srv/playlists` | `/srv/playlists/Musicdrome.m3u` |

`.` is the library root, and it is worth knowing as the fallback that works
almost everywhere: it matches essentially any server's playlist path, and the
entries need no `../` at all. Prefer `.` over leaving the value blank — both
mean the root, but `.` survives every layer of docker's variable substitution
and an empty value does not always.

**This folder used to be `_playlists`, hardcoded.** If yours is still there, it
moves on the next restart — and every path inside it is rewritten for the new
location on the way across. That rewrite is the whole reason this is not a
`mv`: the paths in an `.m3u` are relative to the folder holding it, so a
playlist moved without recomputing them points at nothing, and a music server
imports that as an *empty playlist* rather than reporting it as broken. From
the outside that is indistinguishable from never having been imported. If both
folders somehow hold a playlist, they are merged rather than one silently
winning. Playlists you made yourself are never moved, and the old folder is
only removed once it is empty.

### Navidrome imports nothing

Check these in order.

**1. `ND_PLAYLISTSPATH` and `PLAYLIST_FOLDER` have to agree.** This is the
common one. Unset, `ND_PLAYLISTSPATH` means *every* folder is searched — but
the moment you set it, it means *only* the folders it names. So a playlist
written perfectly to `playlist/` is simply never looked at if Navidrome was
told `Playlists`. Either make the two match, or set `PLAYLIST_FOLDER=.` and
put it at the root.

**2. It is not the folder's name.** Worth ruling out explicitly, because it is
the first thing everyone suspects. Navidrome's skip list, in
`scanner/walk_dir_tree.go`, is exactly `$RECYCLE.BIN`, `#snapshot`,
`@Recycle`, `@Recently-Snapshot`, `.git`, `.streams`, `lost+found`, plus
anything beginning with a single dot. An underscore is not a dot, so
`_playlists` was always scanned like any other folder.

**3. The tracks have to resolve into Navidrome's library.** Entries are
relative to the playlist's own folder, which Navidrome resolves exactly as you
would expect — but only if it mounts your music at the same place Musicdrome
does. If Musicdrome sees `/music` and Navidrome sees `/music/library`, the
`../` climbs out of Navidrome's library and every line resolves to nothing.

**4. Navidrome has to be able to read the file.** Musicdrome runs as root by
default, so `Musicdrome.m3u` is root-owned. If Navidrome runs as another uid,
set `PUID`/`PGID` to match it.

**5. Navidrome only imports on a scan.** `AutoImportPlaylists` defaults to
true, but nothing happens until it next scans. Trigger one from its UI.

Musicdrome names the exact path at boot, so start there:

```
INFO  app.main: playlist: /music/playlist/Musicdrome.m3u
```

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

Musicdrome checks both `/music` and `/config` at boot, and `/music` again before
each download, naming the uid it is running as against the uid that owns the
directory. The container runs as **root by default** precisely so this does not
happen out of the box.

#### Running as yourself instead

Root is the compatible default, not the best one — everything it downloads ends
up owned by `root`, which is a nuisance on the host. Running as your own account
is fully supported and lives entirely in `.env`:

```sh
PUID=1000     # id -u
PGID=1000     # id -g
```

Musicdrome hands the existing `/config` — database, caches, scratch space — to
that uid on the next boot, so switching is not a one-way door and you can switch
back. What it cannot do is change who owns `MUSIC_DIR`, and how you do that
depends on the mount:

| Mount | Fix |
|---|---|
| Local disk, ZFS, unRAID | `sudo chown -R 1000:1000 /path/to/music` |
| CIFS/SMB | Mount with `uid=1000,gid=1000,file_mode=0664,dir_mode=0775` — `chown` does not work on CIFS |
| NFS | Match the uid on the server, or map it with `anonuid` |

On a network share the mount options are the *only* thing that decides
ownership, so setting `uid=`/`gid=` in `/etc/fstab` to match `PUID`/`PGID` is
the whole job — and it is also what makes the files Musicdrome writes land owned
by you rather than by root. Existing files keep whatever ownership they already
had on a local disk; on CIFS they are simply re-presented under the new uid.

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

### "No supported JavaScript runtime could be found"

The same root cause as above, one step earlier. yt-dlp needs an external
JavaScript runtime to answer YouTube's challenges, and when it cannot find a
usable one it does not stop — it falls back to clients that need no JavaScript,
and YouTube answers those with `HTTP Error 403: Forbidden` partway through.

**A runtime that is installed but too old counts as missing.** yt-dlp requires
Deno ≥ 2.3, Node ≥ 22, or QuickJS ≥ 2023-12-9, and anything below the floor is
ignored exactly as if it were absent. Images built before this fix installed
Debian's `nodejs` — 18 on bookworm, 20 on trixie — so the runtime was present,
rejected and never once used. The image now ships Deno, which is yt-dlp's own
recommended runtime, and Musicdrome logs which runtime it found at boot:

```
INFO  app.download: javascript: deno 2.9.5
```

If that line is missing, or an error follows it, downloads will 403.

### "HTTP Error 403: Forbidden"

The search succeeded, the metadata call succeeded, and then the media fetch was
refused. Once the JavaScript runtime above is confirmed working, what is left is
**who is asking**, not what is being asked for. YouTube fingerprints the TLS
handshake and the address it comes from, and blocks VPN and datacenter exits
routinely — so this is common the moment you route downloads through gluetun.

Musicdrome pushes back four ways, all on by default:

| | | Setting |
|---|---|---|
| 1 | The TLS handshake impersonates Chrome, so the connection stops looking like a python script | `YTDLP_IMPERSONATE=chrome` |
| 2 | A 403 mid-download is retried against freshly extracted URLs — a signed media URL that went stale produces exactly this error | `YTDLP_403_RETRIES=2` |
| 3 | Then the next-best candidate is tried, since a different upload of the same track is often served without complaint | — |
| 4 | And after several refusals in a row the queue pauses, rather than converting every remaining track into a failure at dequeue speed | `YTDLP_403_STREAK`, `YTDLP_403_COOLDOWN` |

Impersonation needs [curl_cffi](https://github.com/lexiforest/curl_cffi), which
the image installs as `yt-dlp[default,curl-cffi]`. Install it that way if you
build your own environment, and resist the urge to depend on `curl_cffi`
directly: yt-dlp enforces the version range it was built against at import
time, so an open-ended requirement will eventually install a curl_cffi yt-dlp
refuses to load. When that happens there is no warning — yt-dlp simply reports
zero impersonation targets, and every request fails with:

```
Impersonate target "chrome" is not available. Use --list-impersonate-targets to see available targets.
```

Asking through the extra lets pip resolve whatever yt-dlp currently supports,
at build time and again on every restart. Musicdrome says which it got at boot:

```
INFO  app.main: tls:     impersonating chrome
```

and, when it got nothing, says why rather than leaving you to guess:

```
INFO  app.main: tls:     off — curl_cffi 0.16.0 is installed but yt-dlp will not
load it — Only curl_cffi versions 0.5.10 and 0.10.x through 0.15.x are supported.
```

Downloads continue without impersonation in that state rather than failing
outright, so the worst case is the 403s coming back, not an idle queue.

If downloads still 403 after all of that, the exit address is the problem.
Either route downloads outside the VPN, or give yt-dlp a signed-in identity:
export cookies to a Netscape-format file (`YTDLP_COOKIES_FILE`) or supply a PO
token (`YTDLP_PO_TOKEN`).

### "no confident match on YouTube Music or YouTube"

Not a download failure — nothing was downloaded because nothing credible was
found. Musicdrome refuses to file a track it cannot attribute to the right
artist, on the grounds that a tribute band in your library is worse than a gap.
If it happens constantly, the AI is probably recommending obscure or misspelled
titles; a narrower listening window tends to help.

## When a scan fails

### "could not parse JSON from the model response"

Almost always a local model on Ollama, and almost always the context window
rather than the model. Two things are asked of it, and both are now handled:

**The shape.** Ollama is sent the recommendation *schema*, not just
`"format": "json"`, so the sampler is held to a grammar it cannot leave. Told
only "valid JSON", an 8B model will happily answer with an object keyed by
`"Artist — Title"`, or invent `popularity` and `image_url` fields and spend its
token budget filling them in. Older Ollama builds that reject a schema are
retried the plain way, and the parser accepts the odd shapes anyway.

**The size.** Ollama does not size its context to the request — it uses the
server default, commonly 4096 tokens, and drops whatever does not fit *without
saying so*. A scan sending a long exclusion list and asking for forty
recommendations overflows that, and the reply stops mid-token. Musicdrome now
sizes the window per request, up to `OLLAMA_MAX_NUM_CTX` (16384). Raise it for
large batch sizes, lower it if Ollama is spilling into system RAM, or pin one
value with `OLLAMA_NUM_CTX`.

If a reply is cut off anyway, the recommendations that arrived complete are
kept rather than the scan being thrown away, and the log says so. Lowering
**Tracks per scan** in Settings is the other lever.

## Security

Musicdrome has **no authentication**, by design — it is meant for a trusted home
network. Put it behind a VPN or an authenticating reverse proxy if you need to
reach it from outside.

`NAVIDROME_PASSWORD` is the one real secret in `.env`. It never leaves the
container in a request — only a per-request salted MD5 of it does — and the API
never serves it back: `/api/status` reports the URL, the username and whether
the last sync worked, and nothing else. Navidrome has no scoped tokens, so an
ordinary non-admin Navidrome user is the right account to give it.

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

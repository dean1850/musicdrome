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

**One playlist, kept in step with the library.** Every download lands in
`playlist/Musicdrome.m3u` with relative paths, so the folder can be moved
without breaking it, and a re-download never doubles an entry. Navidrome, Plex
and Jellyfin import it as a single playlist that grows — which is the point:
per-scan playlists produced a wall of `musicdrome-scan-0001`, `-0002`, `-0003`
that nobody opened twice. Old per-scan files are merged into it and deleted the
first time the new image boots. Rename it with `MUSICDROME_PLAYLIST_NAME`.

Deleting a download's file removes its line from the playlist too. An entry
whose file is gone is not inert: a music server imports it as a track, lists it
like any other, and only admits it is missing when somebody presses play.

**Clearing downloads out, in bulk.** Each row in the Downloads tab has a
checkbox, and the one in the column heading takes everything the Show filter
and the search box are currently showing. Shift-click a second box to fill the
range between them. A bar appears above the table with the count and a **Delete
selected**; it deletes the files from disk as well as the rows, prunes the
matching playlist entries, and removes the `Artist/Album` folders left empty
behind them. The library folder itself is never removed, and neither is any
folder that still holds something.

Queued and running downloads cannot be selected. The worker owns those rows,
and deleting one does not stop the download — it only guarantees the file it
finishes writing belongs to nothing. They are left in place and reported as
skipped.

**A deleted track becomes suggestable again**, exactly as removing one at a
time has always done: "Musicdrome already downloaded it" stops being true when
the row goes. If auto-download is on, a later scan may fetch the same music
back. Hide the track from the Discover grid instead when the point is never to
see it again.

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

## Signing downloads in with cookies

Optional, and worth nothing at all until you need it — at which point it is the
only thing that helps. Downloads are anonymous by default, and YouTube tolerates
that from a home address. From a VPN or a datacenter exit it eventually stops
tolerating it and starts answering
[**"Sign in to confirm you're not a bot"**](#sign-in-to-confirm-youre-not-a-bot)
instead, which no amount of retrying gets past.

Cookies are the answer that lets you **stay behind gluetun**: they give YouTube
an account to attach the request to, instead of an anonymous request from an
address it has decided it does not trust.

### Try a different exit address first

Five minutes, nothing to export, and often enough on its own. The endpoints that
get challenged are the popular ones — a busy US or Netherlands exit has thousands
of people behind it and YouTube knows the range. Somewhere unfashionable is
frequently untouched.

Change `SERVER_COUNTRIES` or `SERVER_CITIES` on the **gluetun** service — not in
Musicdrome's `.env`, which does not control the tunnel — then restart **both**:

```bash
docker compose up -d --force-recreate gluetun musicdrome
```

Musicdrome has to come along because it runs inside gluetun's network namespace
(`network_mode: service:gluetun`). Recreating gluetun alone tears that namespace
down underneath a container that is still using it. Then press **Retry all
failed** in the Downloads tab.

This buys time rather than settling anything — the new address is fine until it
is not. Cookies are what stop it coming back, so if you find yourself doing this
a second time, do the section below instead.

### Setting it up

**1. Find the folder.** It is the one you already mount as `/config`, holding
`musicdrome.db` — the `DATA_LOCATION` line in your `.env`. If you are not sure,
ask docker:

```bash
grep DATA_LOCATION .env
# or, straight from the running container:
docker inspect -f '{{range .Mounts}}{{.Source}} → {{.Destination}}{{"\n"}}{{end}}' musicdrome
```

So a `.env` reading `DATA_LOCATION=/home/you/arr/musicdrome/data` means the file
goes at `/home/you/arr/musicdrome/data/cookies.txt`, and Musicdrome sees it as
`/config/cookies.txt`.

**2. Export the cookies.** Install a `cookies.txt` browser extension — one that
exports **Netscape format**, the only format yt-dlp reads. *Get cookies.txt
LOCALLY* (Chrome/Edge) and *cookies.txt* (Firefox) are the usual choices; prefer
one that is open source and does not upload anything, since you are handing it a
live session.

Then do the export in this order, because the details are what make it last:

- Use a **throwaway Google account**, not your main one. This file *is* a live
  session — anyone who reads it is signed in as that account.
- Open a **private/incognito window** and sign in there.
- Go to `youtube.com`, let it finish loading while signed in, and export.
- **Close the window without logging out.** Logging out rotates the session
  server-side and invalidates the file you just exported. This is the single
  most common reason cookies work for an hour and then stop.

**3. Drop the file in.** Name it `cookies.txt` and put it in the folder from
step 1:

```bash
mv ~/Downloads/cookies.txt /home/you/arr/musicdrome/data/cookies.txt
```

That is the whole step. No compose edit, no environment variable, no restart —
`/config` is already mounted, and leaving `YTDLP_COOKIES_FILE` commented out is
correct, because that path is where Musicdrome looks by default. The file is
picked up on the next download, and a queue paused by a bot check resumes within
about five seconds of it landing:

```
INFO  app.download: new cookies — resuming downloads without waiting out the pause
```

Keep the export somewhere else if you prefer — mount it wherever and point
`YTDLP_COOKIES_FILE` at the path *inside* the container. Read-only is fine.

**4. Check it took.** Three quick sanity checks on the file itself. The third is
the one that catches the failure everything else misses:

```bash
head -c 20 /home/you/arr/musicdrome/data/cookies.txt   # text, not "[{"  — JSON is unreadable to yt-dlp
grep -c youtube /home/you/arr/musicdrome/data/cookies.txt        # well above zero
grep -c LOGIN_INFO /home/you/arr/musicdrome/data/cookies.txt     # must be at least 1
```

Then watch the log. There is no need to restart, but if you do, Musicdrome
reports what it found on its own line at boot:

```bash
docker logs -f musicdrome | grep -i cookies
```

```
INFO  app.main: cookies: /config/cookies.txt — signed in, 14 youtube.com cookies, working copy in /config
```

`signed in` is the word to look for. If the file cannot be used at all, the line
says why instead of failing quietly:

```
cookies: not in use — /config/cookies.txt does not exist inside the container — the
         file has to be mounted in, not merely present on the host
cookies: not in use — /config/cookies.txt holds no cookies in Netscape format … export
         it with a cookies.txt extension rather than as JSON
cookies: not in use — /config/cookies.txt has cookies for example.com but none for
         youtube.com — export it with youtube.com open and signed in
cookies: not in use — /config/cookies.txt has 14 youtube.com cookies and every one of
         them has expired — export a fresh one
```

The line to worry about is `cookies: none`. That is not a broken file — it means
there is no file at all, and downloads are still going out anonymously.

Once it reads correctly, press **Retry all failed** in the Downloads tab.

### "I have a cookies.txt and it still says I am a bot"

The one that wastes the most time, because every visible sign says the cookies
are fine. The file mounts, it parses, the log says it is in use, and every
download still fails asking for cookies.

yt-dlp only counts a cookie file as an *account* when the youtube.com jar
carries **`LOGIN_INFO`** *and* at least one of **`SAPISID`**,
**`__Secure-1PAPISID`** or **`__Secure-3PAPISID`**. Short of that it does not
warn and it does not refuse — it downloads anonymously, which puts every request
on the clients YouTube's bot check sits in front of. A jar of a hundred
youtube.com cookies with no `LOGIN_INFO` in it is, as far as YouTube is
concerned, signed out.

Musicdrome checks for exactly those cookies at boot and says so:

```
cookies: read, but downloads are signed out — /config/cookies.txt has cookies, but not
         the ones that sign a session in — yt-dlp wants LOGIN_INFO and one of SAPISID,
         __Secure-1PAPISID, __Secure-3PAPISID, and this export has neither …
```

It is a different message from `not in use` on purpose: the file *is* being read
and handed to yt-dlp, so there is nothing wrong with the mount. What is missing
is the identity.

Three things produce it, in rough order of how often:

- **The export was taken from a signed-out tab.** Common with a private window,
  which is what the instructions above ask for — it is easy to open one, go to
  youtube.com and export *before* signing in. Sign in first, let the page finish
  loading, then export.
- **The extension skipped the HttpOnly cookies.** `LOGIN_INFO` is HttpOnly.
  Extensions that read cookies through the page rather than the browser's cookie
  API cannot see it. Use one that writes `#HttpOnly_` lines — Musicdrome and
  yt-dlp both read that prefix correctly.
- **Only google.com was exported.** `SAPISID` lives on both google.com and
  youtube.com, so a Google export looks convincing and is missing the half that
  matters: `LOGIN_INFO` is never set on google.com.

If the boot line says `signed in` and YouTube is *still* challenging the
connection, then the cookies are not the problem and the exit address is — that
is what a PO token or routing downloads off the VPN is for. The failure message
in the Downloads tab says which of the two it thinks you are looking at.

### Your export is never modified

Worth knowing, because it is the difference between cookies that keep working
and cookies that die overnight. yt-dlp does not only *read* the cookie file — it
**writes it back** when a download finishes, since YouTube rotates the session
cookie as it is used. Pointed at the file you mounted, that goes wrong in both
directions: read-only, the save fails; writable, yt-dlp is editing the only copy
of an export you cannot regenerate without going back to a browser.

So Musicdrome copies your export to a working file (`.cookies-active.txt`, in
the data directory) and hands yt-dlp *that*. Your file is only ever read, the
rotation lands somewhere writable, and the session stays alive across downloads.

There is a second edge on the same knife. yt-dlp writes the jar back by
truncating it and writing it out again — no lock, no temporary file, no atomic
rename — so two downloads finishing at the same moment could leave a jar holding
part of one write. Often enough the part that went missing was `LOGIN_INFO`, and
from then on every download extracted signed out. It presented as cookies that
worked for a while and then stopped, which is indistinguishable from cookies
that genuinely expired.

Each download now gets its own copy of the working file to rewrite, and the
result is moved back over the shared one atomically. Rotation still lands, and
`DOWNLOAD_CONCURRENCY` above 1 can no longer cost you the session.

### When they expire

They will — a few weeks, or sooner if the account signs out anywhere. You will
see the bot check come back. Export again and drop the new file in on top of the
old one; the change is noticed on the next download, and any pause ends early.
Nothing to restart.

Cookies and a PO token together are the strongest combination, and neither helps
if the address is blocked outright — check the boot log's `tls:` line still
reports impersonation before assuming the identity is at fault.

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

### "Sign in to confirm you're not a bot"

The 403's harder cousin, and the usual welcome from a VPN exit that has been
flagged. It is not a 403 and is not handled like one: a 403 is often a single
signed URL that went stale, whereas this is YouTube challenging **the
connection**, so the same track from a different upload, a retry a minute
later, and the next 30 tracks in the queue all get the identical answer.

Musicdrome therefore **pauses the queue on the first one** rather than counting
to three:

```
WARNING app.download: YouTube asked this connection to prove it is not a bot —
pausing downloads for 1800 seconds. This is the identity behind the connection,
not the tracks, and it will not clear on its own.
```

That pause is the difference between one failed track and a Downloads tab where
everything is red — which is what happened before this was recognised: 34
tracks failed in 87 seconds, and `Retry all failed` reproduced it instantly.
The length is `YTDLP_BOT_CHECK_COOLDOWN` (default 1800, `0` disables the pause).

The rest of that message names the lever, and **which lever depends on what you
already have set**: no cookie file, a cookie file that yt-dlp will not draw an
identity from, or cookies that are genuinely signed in and still being
challenged are three different problems with three different fixes, and being
told to add a cookies.txt you mounted last week helps with none of them.

**The pause survives a restart**, and that is deliberate. Restarting is the
first thing anyone does when downloads stop, and it used to clear the pause —
so the container came back, dequeued, and collected the same challenge twenty
seconds later. Restarting does not change the address YouTube refused. Dropping
a new `cookies.txt` in *does*, and that ends the pause immediately, restart or
no restart:

```
INFO  app.download: the cookie file has changed since downloads were paused —
starting without waiting out the rest of it
```

The pause buys time; it does not fix anything, because nothing about your
connection changes while it waits. **[Sign the downloads in with
cookies](#signing-downloads-in-with-cookies)** and they resume on their own,
without leaving the VPN — that is the fix. If the boot line already says
`signed in`, skip ahead: the identity is not what YouTube is objecting to, so
it is the exit address, and the alternatives below are the whole list.

The alternatives, if you would rather not: **[a different exit
address](#try-a-different-exit-address-first)** — another gluetun endpoint or
country, which is the five-minute version and buys time rather than settling it,
or `YTDLP_PROXY` to route only yt-dlp off the VPN — or a PO token via
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider),
which proves the same thing without an account but is more to set up.

### "no confident match on YouTube Music or YouTube"

Not a download failure — nothing was downloaded because nothing credible was
found. Musicdrome refuses to file a track it cannot attribute to the right
artist, on the grounds that a tribute band in your library is worse than a gap.
If it happens constantly, the AI is probably recommending obscure or misspelled
titles; a narrower listening window tends to help.

It no longer hides a blocked search. A YouTube search that comes back with the
bot check above used to be swallowed and reported as an unmatched track, which
filed the one failure that has a fix under the one that does not — that error
now surfaces as itself.

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

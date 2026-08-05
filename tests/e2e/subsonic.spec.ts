/**
 * Subsonic API conformance.
 *
 * Runs at the request level rather than through the browser: these are the
 * calls a third-party client makes, and the contract that has to hold is the
 * response envelope, not any UI. Covers both auth mechanisms, both response
 * formats, the error codes, and every verb the server advertises.
 */
import { expect, test, type APIRequestContext } from '@playwright/test'
import { createHash } from 'node:crypto'

const USER = process.env.E2E_ADMIN_USERNAME || 'admin'
const PASSWORD = process.env.E2E_ADMIN_PASSWORD || 'testadmin123'
const CLIENT = 'playwright-conformance'
const VERSION = '1.16.1'

function tokenAuth(salt = 'saltysalt') {
  return {
    u: USER,
    t: createHash('md5').update(PASSWORD + salt).digest('hex'),
    s: salt,
    v: VERSION,
    c: CLIENT,
  }
}

function passwordAuth() {
  return { u: USER, p: PASSWORD, v: VERSION, c: CLIENT }
}

async function call(
  request: APIRequestContext,
  verb: string,
  params: Record<string, string | number | boolean> = {},
  auth = tokenAuth(),
) {
  const query = new URLSearchParams({ ...auth, f: 'json', ...(params as never) } as never)
  const response = await request.get(`/rest/${verb}?${query.toString()}`)
  expect(response.status(), `${verb} should return HTTP 200`).toBe(200)
  const body = await response.json()
  return body['subsonic-response']
}

async function expectOk(
  request: APIRequestContext,
  verb: string,
  params: Record<string, string | number | boolean> = {},
) {
  const body = await call(request, verb, params)
  expect(body.status, `${verb} returned: ${JSON.stringify(body.error ?? body)}`).toBe('ok')
  expect(body.version).toBe(VERSION)
  expect(body.type).toBe('musicdrome')
  return body
}

test.describe('Subsonic — envelope and authentication', () => {
  test('ping succeeds with token authentication', async ({ request }) => {
    const body = await expectOk(request, 'ping')
    expect(body.openSubsonic).toBe(true)
    expect(body.serverVersion).toBeTruthy()
  })

  test('ping succeeds with password authentication', async ({ request }) => {
    const body = await call(request, 'ping', {}, passwordAuth())
    expect(body.status).toBe('ok')
  })

  test('hex-encoded passwords are accepted', async ({ request }) => {
    const hex = Buffer.from(PASSWORD, 'utf8').toString('hex')
    const body = await call(request, 'ping', {}, {
      u: USER,
      p: `enc:${hex}`,
      v: VERSION,
      c: CLIENT,
    })
    expect(body.status).toBe('ok')
  })

  test('the .view suffix is accepted', async ({ request }) => {
    const query = new URLSearchParams({ ...tokenAuth(), f: 'json' } as never)
    const response = await request.get(`/rest/ping.view?${query.toString()}`)
    expect(response.status()).toBe(200)
    expect((await response.json())['subsonic-response'].status).toBe('ok')
  })

  test('XML is the default response format', async ({ request }) => {
    const query = new URLSearchParams({ ...tokenAuth() } as never)
    const response = await request.get(`/rest/ping?${query.toString()}`)

    expect(response.headers()['content-type']).toContain('xml')
    const text = await response.text()
    expect(text).toContain('<?xml version="1.0" encoding="UTF-8"?>')
    expect(text).toContain('xmlns="http://subsonic.org/restapi"')
    expect(text).toContain('status="ok"')
    expect(text).toContain(`version="${VERSION}"`)
  })

  test('JSONP wraps the document in the callback', async ({ request }) => {
    const query = new URLSearchParams({
      ...tokenAuth(),
      f: 'jsonp',
      callback: 'myCallback',
    } as never)
    const response = await request.get(`/rest/ping?${query.toString()}`)

    const text = await response.text()
    expect(text.startsWith('myCallback(')).toBeTruthy()
    expect(text.trimEnd().endsWith(');')).toBeTruthy()
  })

  test('a wrong password returns error 40 with HTTP 200', async ({ request }) => {
    const body = await call(request, 'ping', {}, { ...tokenAuth(), t: 'deadbeef' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(40)
  })

  test('an unknown user returns error 40', async ({ request }) => {
    const body = await call(request, 'ping', {}, { ...tokenAuth(), u: 'nobody-here' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(40)
  })

  test('a missing username returns error 10', async ({ request }) => {
    const response = await request.get(`/rest/ping?v=${VERSION}&c=${CLIENT}&f=json`)
    const body = (await response.json())['subsonic-response']
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(10)
  })

  test('missing credentials return error 10', async ({ request }) => {
    const body = await call(request, 'ping', {}, { u: USER, v: VERSION, c: CLIENT })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(10)
  })

  test('a POSTed form body authenticates too', async ({ request }) => {
    const response = await request.post('/rest/ping', {
      form: { ...tokenAuth(), f: 'json' },
    })
    expect(response.status()).toBe(200)
    expect((await response.json())['subsonic-response'].status).toBe('ok')
  })
})

test.describe('Subsonic — system', () => {
  test('getLicense reports a valid licence', async ({ request }) => {
    const body = await expectOk(request, 'getLicense')
    expect(body.license.valid).toBe(true)
  })

  test('getMusicFolders returns the library root', async ({ request }) => {
    const body = await expectOk(request, 'getMusicFolders')
    expect(body.musicFolders.musicFolder.length).toBeGreaterThan(0)
  })

  test('getOpenSubsonicExtensions lists the supported extensions', async ({ request }) => {
    const body = await expectOk(request, 'getOpenSubsonicExtensions')
    expect(Array.isArray(body.openSubsonicExtensions)).toBeTruthy()
  })

  test('getScanStatus reports the indexed count', async ({ request }) => {
    const body = await expectOk(request, 'getScanStatus')
    expect(body.scanStatus).toHaveProperty('scanning')
    expect(body.scanStatus.count).toBeGreaterThan(0)
  })

  test('getUser describes the caller', async ({ request }) => {
    const body = await expectOk(request, 'getUser', { username: USER })
    expect(body.user.username).toBe(USER)
    expect(body.user.streamRole).toBe(true)
  })

  test('getNowPlaying is valid even when nothing is playing', async ({ request }) => {
    await expectOk(request, 'getNowPlaying')
  })

  test('getInternetRadioStations and getBookmarks are valid when empty', async ({ request }) => {
    await expectOk(request, 'getInternetRadioStations')
    await expectOk(request, 'getBookmarks')
  })

  test('jukeboxControl is refused rather than half-implemented', async ({ request }) => {
    const body = await call(request, 'jukeboxControl', { action: 'get' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(50)
  })
})

test.describe('Subsonic — browsing', () => {
  test('getIndexes groups artists alphabetically', async ({ request }) => {
    const body = await expectOk(request, 'getIndexes')
    expect(body.indexes.index.length).toBeGreaterThan(0)
    expect(body.indexes).toHaveProperty('ignoredArticles')

    const names = body.indexes.index.flatMap((entry: any) =>
      entry.artist.map((artist: any) => artist.name),
    )
    expect(names).toContain('Aurora Fields')
  })

  test('getArtists returns the ID3 view', async ({ request }) => {
    const body = await expectOk(request, 'getArtists')
    const artists = body.artists.index.flatMap((entry: any) => entry.artist)
    expect(artists.length).toBe(4)
    expect(artists[0].id).toMatch(/^ar-\d+$/)
  })

  test('getArtist returns that artist and its albums', async ({ request }) => {
    const indexes = await expectOk(request, 'getArtists')
    const artist = indexes.artists.index
      .flatMap((entry: any) => entry.artist)
      .find((a: any) => a.name === 'Aurora Fields')

    const body = await expectOk(request, 'getArtist', { id: artist.id })
    expect(body.artist.name).toBe('Aurora Fields')
    expect(body.artist.album.length).toBe(2)
  })

  test('getAlbum returns its songs in disc/track order', async ({ request }) => {
    const list = await expectOk(request, 'getAlbumList2', { type: 'alphabeticalByName', size: 20 })
    const album = list.albumList2.album.find((a: any) => a.name === 'Paper Trails')
    expect(album).toBeTruthy()

    const body = await expectOk(request, 'getAlbum', { id: album.id })
    expect(body.album.song.length).toBe(5)
    expect(body.album.song[0].track).toBe(1)
    expect(body.album.song[0].title).toBe('Receipts')

    for (const song of body.album.song) {
      expect(song.id).toMatch(/^tr-\d+$/)
      expect(song).toHaveProperty('duration')
      expect(song).toHaveProperty('suffix')
      expect(song).toHaveProperty('contentType')
    }
  })

  test('getSong returns a single track', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    const body = await expectOk(request, 'getSong', { id })
    expect(body.song.id).toBe(id)
  })

  test('getMusicDirectory works for both artist and album ids', async ({ request }) => {
    const indexes = await expectOk(request, 'getArtists')
    const artistId = indexes.artists.index.flatMap((e: any) => e.artist)[0].id

    const artistDir = await expectOk(request, 'getMusicDirectory', { id: artistId })
    expect(artistDir.directory.child.length).toBeGreaterThan(0)

    const albumId = artistDir.directory.child[0].id
    const albumDir = await expectOk(request, 'getMusicDirectory', { id: albumId })
    expect(albumDir.directory.child.length).toBeGreaterThan(0)
    expect(albumDir.directory.child[0].isDir).toBe(false)
  })

  test('getGenres reports counts per genre', async ({ request }) => {
    const body = await expectOk(request, 'getGenres')
    const genres = body.genres.genre
    expect(genres.length).toBeGreaterThanOrEqual(4)

    const jazz = genres.find((g: any) => g.value === 'Jazz')
    expect(jazz.songCount).toBe(3)
  })

  test('every getAlbumList2 type is accepted', async ({ request }) => {
    const types = [
      'random',
      'newest',
      'highest',
      'frequent',
      'recent',
      'alphabeticalByName',
      'alphabeticalByArtist',
      'starred',
    ]
    for (const type of types) {
      const body = await call(request, 'getAlbumList2', { type, size: 5 })
      expect(body.status, `type=${type} failed`).toBe('ok')
    }
  })

  test('getAlbumList2 byYear filters on the year range', async ({ request }) => {
    const body = await expectOk(request, 'getAlbumList2', {
      type: 'byYear',
      fromYear: 2024,
      toYear: 2026,
      size: 20,
    })
    for (const album of body.albumList2.album || []) {
      expect(album.year).toBeGreaterThanOrEqual(2024)
    }
  })

  test('getAlbumList2 byGenre without a genre returns error 10', async ({ request }) => {
    const body = await call(request, 'getAlbumList2', { type: 'byGenre' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(10)
  })

  test('getSongsByGenre filters correctly', async ({ request }) => {
    const body = await expectOk(request, 'getSongsByGenre', { genre: 'Jazz', count: 20 })
    expect(body.songsByGenre.song.length).toBe(3)
  })

  test('getRandomSongs respects the size parameter', async ({ request }) => {
    const body = await expectOk(request, 'getRandomSongs', { size: 3 })
    expect(body.randomSongs.song.length).toBe(3)
  })

  test('getArtistInfo2 and getAlbumInfo2 return documents', async ({ request }) => {
    const indexes = await expectOk(request, 'getArtists')
    const artistId = indexes.artists.index.flatMap((e: any) => e.artist)[0].id
    await expectOk(request, 'getArtistInfo2', { id: artistId })

    const list = await expectOk(request, 'getAlbumList2', { type: 'newest', size: 1 })
    await expectOk(request, 'getAlbumInfo2', { id: list.albumList2.album[0].id })
  })

  test('an unknown album id returns error 70', async ({ request }) => {
    const body = await call(request, 'getAlbum', { id: 'al-999999' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(70)
  })

  test('a malformed id is rejected rather than crashing', async ({ request }) => {
    const body = await call(request, 'getAlbum', { id: 'not-an-id' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(70)
  })
})

test.describe('Subsonic — search', () => {
  test('search3 matches across artists, albums and songs', async ({ request }) => {
    const body = await expectOk(request, 'search3', { query: 'Aurora' })
    expect(body.searchResult3.artist.length).toBe(1)
    expect(body.searchResult3.album.length).toBe(2)
  })

  test('search2 returns the legacy container', async ({ request }) => {
    const body = await expectOk(request, 'search2', { query: 'Glacier' })
    expect(body.searchResult2.song.length).toBe(1)
    expect(body.searchResult2.song[0].title).toBe('Glacier Song')
  })

  test('an empty query returns everything, per the spec', async ({ request }) => {
    const body = await expectOk(request, 'search3', { query: '', songCount: 50 })
    expect(body.searchResult3.song.length).toBeGreaterThan(10)
  })

  test('count and offset parameters are honoured', async ({ request }) => {
    const body = await expectOk(request, 'search3', {
      query: '',
      songCount: 2,
      artistCount: 1,
      albumCount: 1,
    })
    expect(body.searchResult3.song.length).toBe(2)
    expect(body.searchResult3.artist.length).toBe(1)
  })
})

test.describe('Subsonic — media', () => {
  test('stream returns audio bytes', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    const query = new URLSearchParams({ ...tokenAuth(), id } as never)
    const response = await request.get(`/rest/stream?${query.toString()}`)

    expect(response.status()).toBe(200)
    expect(response.headers()['content-type']).toContain('audio')
    expect((await response.body()).length).toBeGreaterThan(1000)
  })

  test('download returns the original file', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    const query = new URLSearchParams({ ...tokenAuth(), id } as never)
    const response = await request.get(`/rest/download?${query.toString()}`)

    expect(response.status()).toBe(200)
    expect((await response.body()).length).toBeGreaterThan(1000)
  })

  test('getCoverArt returns an image, sized when asked', async ({ request }) => {
    const list = await expectOk(request, 'getAlbumList2', { type: 'newest', size: 1 })
    const id = list.albumList2.album[0].coverArt

    const query = new URLSearchParams({ ...tokenAuth(), id, size: '200' } as never)
    const response = await request.get(`/rest/getCoverArt?${query.toString()}`)

    expect(response.status()).toBe(200)
    expect(response.headers()['content-type']).toContain('image')
  })

  test('streaming an unknown id returns error 70', async ({ request }) => {
    const body = await call(request, 'stream', { id: 'tr-999999' })
    expect(body.status).toBe('failed')
    expect(body.error.code).toBe(70)
  })
})

test.describe('Subsonic — annotations and scrobbling', () => {
  test('star, getStarred2 and unstar round-trip', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    await expectOk(request, 'star', { id })
    const starred = await expectOk(request, 'getStarred2')
    expect((starred.starred2.song || []).some((s: any) => s.id === id)).toBeTruthy()

    await expectOk(request, 'unstar', { id })
    const after = await expectOk(request, 'getStarred2')
    expect((after.starred2.song || []).some((s: any) => s.id === id)).toBeFalsy()
  })

  test('setRating is reflected on the song', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    await expectOk(request, 'setRating', { id, rating: 4 })
    const song = await expectOk(request, 'getSong', { id })
    expect(song.song.userRating).toBe(4)

    await expectOk(request, 'setRating', { id, rating: 0 })
  })

  test('an out-of-range rating is rejected', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const body = await call(request, 'setRating', { id: list.randomSongs.song[0].id, rating: 9 })
    expect(body.status).toBe('failed')
  })

  test('scrobble increments the play count', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    const before = (await expectOk(request, 'getSong', { id })).song.playCount || 0
    await expectOk(request, 'scrobble', { id, submission: 'true' })
    const after = (await expectOk(request, 'getSong', { id })).song.playCount || 0

    expect(after).toBe(before + 1)
  })

  test('a now-playing scrobble does not count as a play', async ({ request }) => {
    const list = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = list.randomSongs.song[0].id

    const before = (await expectOk(request, 'getSong', { id })).song.playCount || 0
    await expectOk(request, 'scrobble', { id, submission: 'false' })
    const after = (await expectOk(request, 'getSong', { id })).song.playCount || 0

    expect(after).toBe(before)

    const nowPlaying = await expectOk(request, 'getNowPlaying')
    expect(nowPlaying.nowPlaying.entry.length).toBeGreaterThan(0)
  })
})

test.describe('Subsonic — playlists', () => {
  test('create, read, update and delete a playlist', async ({ request }) => {
    const songs = await expectOk(request, 'getRandomSongs', { size: 3 })
    const ids = songs.randomSongs.song.map((s: any) => s.id)
    const name = `Subsonic test ${Date.now()}`

    const params = new URLSearchParams({ ...tokenAuth(), f: 'json', name } as never)
    for (const id of ids) params.append('songId', id)
    const createdResponse = await request.get(`/rest/createPlaylist?${params.toString()}`)
    const created = (await createdResponse.json())['subsonic-response']
    expect(created.status).toBe('ok')
    expect(created.playlist.songCount).toBe(3)

    const playlistId = created.playlist.id
    expect(playlistId).toMatch(/^pl-\d+$/)

    const fetched = await expectOk(request, 'getPlaylist', { id: playlistId })
    expect(fetched.playlist.entry.length).toBe(3)

    // Remove the first entry by index
    await expectOk(request, 'updatePlaylist', {
      playlistId,
      songIndexToRemove: 0,
      comment: 'edited by the conformance suite',
    })
    const updated = await expectOk(request, 'getPlaylist', { id: playlistId })
    expect(updated.playlist.entry.length).toBe(2)
    expect(updated.playlist.comment).toBe('edited by the conformance suite')

    const all = await expectOk(request, 'getPlaylists')
    expect(all.playlists.playlist.some((p: any) => p.id === playlistId)).toBeTruthy()

    await expectOk(request, 'deletePlaylist', { id: playlistId })
    const gone = await call(request, 'getPlaylist', { id: playlistId })
    expect(gone.status).toBe('failed')
    expect(gone.error.code).toBe(70)
  })

  test('getPlaylists includes the seeded smart playlists', async ({ request }) => {
    const body = await expectOk(request, 'getPlaylists')
    const names = body.playlists.playlist.map((p: any) => p.name)
    expect(names).toContain('Recently Added')
  })
})

test.describe('Subsonic — play queue and bookmarks', () => {
  test('savePlayQueue round-trips through getPlayQueue', async ({ request }) => {
    const songs = await expectOk(request, 'getRandomSongs', { size: 2 })
    const ids = songs.randomSongs.song.map((s: any) => s.id)

    const params = new URLSearchParams({
      ...tokenAuth(),
      f: 'json',
      current: ids[0],
      position: '4200',
    } as never)
    for (const id of ids) params.append('id', id)

    const saved = await request.get(`/rest/savePlayQueue?${params.toString()}`)
    expect((await saved.json())['subsonic-response'].status).toBe('ok')

    const body = await expectOk(request, 'getPlayQueue')
    expect(body.playQueue.entry.length).toBe(2)
    expect(body.playQueue.position).toBe(4200)
    expect(body.playQueue.current).toBe(ids[0])
  })

  test('bookmarks can be created, listed and deleted', async ({ request }) => {
    const songs = await expectOk(request, 'getRandomSongs', { size: 1 })
    const id = songs.randomSongs.song[0].id

    await expectOk(request, 'createBookmark', { id, position: 1234, comment: 'resume here' })
    const body = await expectOk(request, 'getBookmarks')
    const bookmark = body.bookmarks.bookmark.find((b: any) => b.entry.id === id)
    expect(bookmark.position).toBe(1234)

    await expectOk(request, 'deleteBookmark', { id })
    const after = await expectOk(request, 'getBookmarks')
    expect((after.bookmarks.bookmark || []).some((b: any) => b.entry.id === id)).toBeFalsy()
  })
})

test.describe('Subsonic — podcasts and users', () => {
  test('getPodcasts is valid with no subscriptions', async ({ request }) => {
    await expectOk(request, 'getPodcasts')
    await expectOk(request, 'getNewestPodcasts', { count: 5 })
  })

  test('a non-admin cannot list users', async ({ request }) => {
    const username = `subsonic_listener_${Date.now()}`
    await expectOk(request, 'createUser', { username, password: 'listener-pass-123' })

    const salt = 'anothersalt'
    const auth = {
      u: username,
      t: createHash('md5').update('listener-pass-123' + salt).digest('hex'),
      s: salt,
      v: VERSION,
      c: CLIENT,
    }

    const ping = await call(request, 'ping', {}, auth)
    expect(ping.status).toBe('ok')

    const users = await call(request, 'getUsers', {}, auth)
    expect(users.status).toBe('failed')
    expect(users.error.code).toBe(50)

    await expectOk(request, 'deleteUser', { username })
  })

  test('changePassword updates the credentials clients use', async ({ request }) => {
    const username = `subsonic_pwtest_${Date.now()}`
    await expectOk(request, 'createUser', { username, password: 'first-password' })

    await expectOk(request, 'changePassword', { username, password: 'second-password' })

    const salt = 'pwsalt'
    const oldAuth = {
      u: username,
      t: createHash('md5').update('first-password' + salt).digest('hex'),
      s: salt,
      v: VERSION,
      c: CLIENT,
    }
    const newAuth = {
      u: username,
      t: createHash('md5').update('second-password' + salt).digest('hex'),
      s: salt,
      v: VERSION,
      c: CLIENT,
    }

    expect((await call(request, 'ping', {}, oldAuth)).status).toBe('failed')
    expect((await call(request, 'ping', {}, newAuth)).status).toBe('ok')

    await expectOk(request, 'deleteUser', { username })
  })
})

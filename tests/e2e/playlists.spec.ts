import { expect, type Page, test } from '@playwright/test'
import { gotoTab, login } from './helpers'

/**
 * Scope name lookups to the card grid — the success toast repeats the playlist
 * name, so a bare getByText would match two elements.
 */
const card = (page: Page, name: string) =>
  page.getByTestId('playlist-card').filter({ hasText: name })

async function createPlaylist(page: Page, name: string) {
  await page.getByTestId('new-playlist').click()
  await page.getByTestId('playlist-name').fill(name)
  await page.getByTestId('create-playlist').click()
  await expect(card(page, name)).toBeVisible()
}

test.beforeEach(async ({ page }) => {
  await login(page)
  await gotoTab(page, 'playlists')
  await expect(page.getByTestId('playlists-page')).toBeVisible()
})

test.describe('playlists', () => {
  test('the starter smart playlists are seeded for a new account', async ({ page }) => {
    await page.getByTestId('tab-smart').click()
    await expect(page.getByTestId('playlist-card').first()).toBeVisible()
    await expect(card(page, 'Recently Added')).toBeVisible()
    await expect(card(page, 'Never Played')).toBeVisible()
  })

  test('creates a manual playlist', async ({ page }) => {
    const name = `Road trip ${Date.now()}`
    await createPlaylist(page, name)
    await expect(page.getByTestId('toast')).toBeVisible()
  })

  test('adds a track to a playlist and sees it in the detail view', async ({ page }) => {
    const name = `Mixtape ${Date.now()}`
    await createPlaylist(page, name)

    // Add a track from an album page
    await gotoTab(page, 'albums')
    await page.getByText('Northern Lights').click()
    const row = page.getByTestId('track-row').first()
    await row.hover()
    await row.getByTestId('add-to-playlist').click()

    await expect(page.getByTestId('modal')).toBeVisible()
    await page.getByTestId('modal').getByRole('button', { name: new RegExp(name) }).click()

    await gotoTab(page, 'playlists')
    await card(page, name).click()
    await expect(page.getByTestId('playlist-title')).toHaveText(name)
    await expect(page.getByTestId('track-row')).toHaveCount(1)
    await expect(page.getByTestId('track-row').first()).toContainText('First Light')
  })

  test('plays a playlist', async ({ page }) => {
    await page.getByTestId('tab-smart').click()
    await card(page, 'Never Played').click()

    await expect(page.getByTestId('playlist-detail')).toBeVisible()
    await page.getByTestId('play-playlist').click()
    await expect(page.getByTestId('now-playing-title')).not.toBeEmpty()
  })

  test('creates a smart playlist from a rule document', async ({ page }) => {
    const name = `Jazz only ${Date.now()}`

    await page.getByTestId('new-smart').click()
    await page.getByTestId('smart-name').fill(name)
    await page
      .getByTestId('smart-rules')
      .fill('{"all": [{"is": {"genre": "Jazz"}}], "sort": "title", "limit": 50}')
    await page.getByTestId('create-smart').click()

    await expect(card(page, name)).toBeVisible()
    await card(page, name).click()

    // Exactly the three jazz tracks in the seeded library
    await expect(page.getByTestId('track-row')).toHaveCount(3)
    await expect(page.getByTestId('track-row').first()).toContainText('Nadia Okonkwo')
  })

  test('rejects an invalid smart-playlist rule document', async ({ page }) => {
    await page.getByTestId('new-smart').click()
    await page.getByTestId('smart-name').fill('Broken rules')
    await page.getByTestId('smart-rules').fill('{"all": [{"notAnOperator": {"genre": "Jazz"}}]}')
    await page.getByTestId('create-smart').click()

    await expect(page.getByTestId('error-banner')).toBeVisible()
    await expect(page.getByTestId('error-banner')).toContainText(/unknown operator/i)
  })

  test('refreshes a smart playlist after the library changes', async ({ page }) => {
    await page.getByTestId('tab-smart').click()
    await card(page, 'Never Played').click()

    await page.getByTestId('refresh-playlist').click()
    await expect(page.getByTestId('toast')).toContainText('refreshed')
  })

  test('deletes a playlist', async ({ page }) => {
    const name = `Temporary ${Date.now()}`
    await createPlaylist(page, name)

    await card(page, name).click()
    await page.getByTestId('delete-playlist').click()
    await page.getByTestId('confirm-delete').click()

    await expect(page.getByTestId('playlists-page')).toBeVisible()
    await expect(card(page, name)).toHaveCount(0)
  })

  test('the AI curation button is disabled when no provider is configured', async ({ page }) => {
    // The E2E server runs with AI_ENABLED=false
    await expect(page.getByTestId('new-ai')).toBeDisabled()
  })

  test('renames a playlist and reorders its tracks', async ({ page }) => {
    const name = `Sortable ${Date.now()}`
    await createPlaylist(page, name)

    // Two tracks, added from an album page
    await gotoTab(page, 'albums')
    await page.getByText('Test Patterns').click()
    for (const index of [0, 1]) {
      const row = page.getByTestId('track-row').nth(index)
      await row.hover()
      await row.getByTestId('add-to-playlist').click()
      await page.getByTestId('modal').getByRole('button', { name: new RegExp(name) }).click()
      await expect(page.getByTestId('modal')).toBeHidden()
    }

    await gotoTab(page, 'playlists')
    await card(page, name).click()
    await expect(page.getByTestId('track-row')).toHaveCount(2)
    await expect(page.getByTestId('track-row').first()).toContainText('Colour Bars')

    // Keyboard reordering — the same path the drag handle drives
    await page.getByTestId('reorder-track').first().focus()
    await page.keyboard.press('ArrowDown')
    await expect(page.getByTestId('track-row').first()).toContainText('Vertical Hold')

    // Removing a track
    const row = page.getByTestId('track-row').first()
    await row.hover()
    await row.getByTestId('remove-track').click()
    await expect(page.getByTestId('track-row')).toHaveCount(1)

    // Renaming
    const renamed = `${name} renamed`
    await page.getByTestId('edit-playlist').click()
    await page.getByTestId('edit-playlist-name').fill(renamed)
    await page.getByTestId('save-playlist').click()
    await expect(page.getByTestId('playlist-title')).toHaveText(renamed)
  })
})

test.describe('m3u playlists', () => {
  test('imports the .m3u file seeded in the library', async ({ page }) => {
    await page.getByTestId('tab-imported').click()
    await expect(card(page, 'Downtify Mix')).toBeVisible()

    await card(page, 'Downtify Mix').click()
    await expect(page.getByTestId('playlist-title')).toHaveText('Downtify Mix')

    // Two of the three entries are in the library; the third is not
    await expect(page.getByTestId('track-row')).toHaveCount(2)
    await expect(page.getByTestId('track-row').first()).toContainText('Colour Bars')
    await expect(page.getByTestId('track-row').nth(1)).toContainText('Copper Rain')
    await expect(page.getByTestId('import-missing')).toContainText('1')
    await expect(page.getByTestId('import-info')).toContainText('Downtify Mix.m3u')
  })

  test('imports an uploaded .m3u file', async ({ page }) => {
    await page.getByTestId('import-m3u').click()
    await page.getByTestId('import-upload').setInputFiles({
      name: 'Uploaded Mix.m3u',
      mimeType: 'audio/x-mpegurl',
      buffer: Buffer.from(
        '#EXTM3U\n' +
          '#PLAYLIST:Uploaded Mix\n' +
          '#EXTINF:2,Aurora Fields - First Light\n' +
          'Aurora Fields/Northern Lights/01 - First Light.wav\n',
      ),
    })

    await expect(page.getByTestId('toast')).toContainText('Uploaded Mix')
    await page.getByTestId('tab-imported').click()
    await card(page, 'Uploaded Mix').click()
    await expect(page.getByTestId('track-row')).toHaveCount(1)
    await expect(page.getByTestId('track-row').first()).toContainText('First Light')
  })

  test('re-scans the library for playlist files on demand', async ({ page }) => {
    await page.getByTestId('import-m3u').click()
    await page.getByTestId('import-scan').click()
    await expect(page.getByTestId('toast')).toContainText('playlist file')
  })
})

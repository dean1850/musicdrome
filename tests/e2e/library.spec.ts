import { expect, test } from '@playwright/test'
import { gotoTab, login } from './helpers'

test.beforeEach(async ({ page }) => {
  await login(page)
})

test.describe('library browsing', () => {
  test('home shows library statistics once scanned', async ({ page }) => {
    const home = page.getByTestId('home-page')
    await expect(home).toBeVisible()

    // The seeded library has 19 tracks across 5 albums by 4 artists
    await expect(home.getByTestId('stat-tracks')).toContainText('19')
    await expect(home.getByTestId('stat-albums')).toContainText('5')
    await expect(home.getByTestId('stat-artists')).toContainText('4')
    await expect(home.getByRole('heading', { name: 'Recently added' })).toBeVisible()
    await expect(page.getByTestId('album-card').first()).toBeVisible()
  })

  test('albums page lists the seeded albums', async ({ page }) => {
    await gotoTab(page, 'albums')
    await expect(page.getByTestId('albums-page')).toBeVisible()

    const cards = page.getByTestId('album-card')
    await expect(cards).toHaveCount(5)
    await expect(page.getByText('Northern Lights')).toBeVisible()
    await expect(page.getByText('Paper Trails')).toBeVisible()
  })

  test('albums can be filtered by name', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByTestId('album-filter').fill('Paper')

    await expect(page.getByTestId('album-card')).toHaveCount(1)
    await expect(page.getByText('Paper Trails')).toBeVisible()
  })

  test('album detail lists its tracks in order', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Paper Trails').click()

    await expect(page.getByTestId('album-detail')).toBeVisible()
    await expect(page.getByTestId('album-title')).toHaveText('Paper Trails')

    const rows = page.getByTestId('track-row')
    await expect(rows).toHaveCount(5)
    await expect(rows.first()).toContainText('Receipts')
    await expect(rows.last()).toContainText('Closing Entry')
  })

  test('artists page lists the seeded artists', async ({ page }) => {
    await gotoTab(page, 'artists')
    await expect(page.getByTestId('artists-page')).toBeVisible()
    await expect(page.getByTestId('artist-card')).toHaveCount(4)
    await expect(page.getByText('Aurora Fields')).toBeVisible()
  })

  test('artist detail shows albums and switches to tracks', async ({ page }) => {
    await gotoTab(page, 'artists')
    await page.getByText('Aurora Fields').click()

    await expect(page.getByTestId('artist-name')).toHaveText('Aurora Fields')
    // Aurora Fields has two albums in the seeded library
    await expect(page.getByTestId('album-card')).toHaveCount(2)

    await page.getByTestId('tab-tracks').click()
    await expect(page.getByTestId('track-row')).toHaveCount(7)
  })

  test('navigating from a track to its album works', async ({ page }) => {
    await gotoTab(page, 'tracks')
    await expect(page.getByTestId('tracks-page')).toBeVisible()

    await page.getByRole('link', { name: 'Northern Lights' }).first().click()
    await expect(page.getByTestId('album-title')).toHaveText('Northern Lights')
  })

  test('genre filter narrows the track list', async ({ page }) => {
    await gotoTab(page, 'tracks')
    await page.getByLabel('Genre').selectOption('Jazz')

    const rows = page.getByTestId('track-row')
    await expect(rows).toHaveCount(3)
    await expect(rows.first()).toContainText('Nadia Okonkwo')
  })
})

test.describe('search', () => {
  test('finds artists, albums and tracks', async ({ page }) => {
    await page.getByTestId('global-search').fill('Aurora')
    await page.getByTestId('global-search').press('Enter')

    await expect(page.getByTestId('search-page')).toBeVisible()
    await expect(page.getByTestId('artist-card')).toHaveCount(1)
    await expect(page.getByTestId('album-card')).toHaveCount(2)
  })

  test('matches on track title', async ({ page }) => {
    await page.getByTestId('global-search').fill('Glacier')
    await page.getByTestId('global-search').press('Enter')

    await expect(page.getByTestId('track-row')).toHaveCount(1)
    await expect(page.getByTestId('track-row').first()).toContainText('Glacier Song')
  })

  test('reports when nothing matches', async ({ page }) => {
    await page.getByTestId('global-search').fill('zzzznotathing')
    await page.getByTestId('global-search').press('Enter')

    await expect(page.getByTestId('empty-state')).toBeVisible()
    await expect(page.getByText('Nothing matched')).toBeVisible()
  })

  test('the / shortcut focuses the search box', async ({ page }) => {
    await page.locator('body').press('/')
    await expect(page.getByTestId('global-search')).toBeFocused()
  })
})

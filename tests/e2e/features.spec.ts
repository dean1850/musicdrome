import { expect, test } from '@playwright/test'
import { gotoTab, login } from './helpers'

test.beforeEach(async ({ page }) => {
  await login(page)
})

test.describe('analytics', () => {
  test('renders statistics once there is listening history', async ({ page }) => {
    // Generate a play so the page has something to chart
    await gotoTab(page, 'albums')
    await page.getByText('Northern Lights').click()
    await page.getByTestId('play-album').click()
    await page.waitForTimeout(3500)

    await gotoTab(page, 'analytics')
    await expect(page.getByTestId('analytics-page')).toBeVisible()
    await expect(page.getByText('Plays', { exact: true })).toBeVisible()
    await expect(page.getByText('Listening clock')).toBeVisible()
    await expect(page.getByText('Top artists')).toBeVisible()
  })

  test('switching the period reloads the figures', async ({ page }) => {
    await gotoTab(page, 'analytics')
    await page.getByTestId('tab-week').click()
    await expect(page.getByTestId('tab-week')).toHaveAttribute('aria-selected', 'true')
    await page.getByTestId('tab-year').click()
    await expect(page.getByTestId('tab-year')).toHaveAttribute('aria-selected', 'true')
  })
})

test.describe('discover', () => {
  test('shows the recommendations and wanted tabs', async ({ page }) => {
    await gotoTab(page, 'discover')
    await expect(page.getByTestId('discover-page')).toBeVisible()
    await expect(page.getByTestId('tab-recommendations')).toBeVisible()
    await expect(page.getByTestId('tab-wanted')).toBeVisible()
  })

  test('adds an item to the wanted queue by hand', async ({ page }) => {
    await gotoTab(page, 'discover')
    await page.getByTestId('tab-wanted').click()

    await page.getByTestId('add-wanted').click()
    await page.getByTestId('wanted-artist').fill('Test Artist')
    await page.getByTestId('submit-wanted').click()

    await expect(page.getByTestId('wanted-item').first()).toBeVisible()
    await expect(page.getByTestId('wanted-item').first()).toContainText('Test Artist')
  })
})

test.describe('podcasts', () => {
  test('shows an empty state before subscribing', async ({ page }) => {
    await gotoTab(page, 'podcasts')
    await expect(page.getByTestId('podcasts-page')).toBeVisible()
    await expect(page.getByTestId('empty-state')).toBeVisible()
  })

  test('reports a helpful error for an unreachable feed', async ({ page }) => {
    await gotoTab(page, 'podcasts')
    await page.getByTestId('add-podcast').click()
    await page.getByTestId('podcast-url').fill('http://127.0.0.1:9/nope.xml')
    await page.getByTestId('submit-podcast').click()

    await expect(page.getByTestId('error-banner')).toBeVisible()
  })
})

test.describe('settings', () => {
  test('saves a playback preference', async ({ page }) => {
    await page.getByTestId('nav-settings').click()
    await expect(page.getByTestId('settings-page')).toBeVisible()

    await page.getByTestId('max-bitrate').selectOption('192')
    await page.getByTestId('save-playback').click()
    await expect(page.getByTestId('toast')).toBeVisible()

    await page.reload()
    await expect(page.getByTestId('max-bitrate')).toHaveValue('192')
  })

  test('shows the Subsonic connection details', async ({ page }) => {
    await page.getByTestId('nav-settings').click()
    await expect(page.getByTestId('subsonic-url')).toBeVisible()
    await expect(page.getByText('Connect a Subsonic client')).toBeVisible()
  })
})

test.describe('admin', () => {
  test('library tab reports the scan state', async ({ page }) => {
    await page.getByTestId('nav-admin').click()
    await expect(page.getByTestId('admin-page')).toBeVisible()
    await expect(page.getByText('Library scan')).toBeVisible()
    await expect(page.getByTestId('scan-quick')).toBeVisible()
  })

  test('starts a scan', async ({ page }) => {
    await page.getByTestId('nav-admin').click()
    await page.getByTestId('scan-quick').click()
    await expect(page.getByTestId('toast')).toBeVisible()
  })

  test('creates a user and lists it', async ({ page }) => {
    const username = `member_${Date.now()}`

    await page.getByTestId('nav-admin').click()
    await page.getByTestId('tab-users').click()
    await page.getByTestId('add-user').click()

    await page.getByTestId('new-username').fill(username)
    await page.getByTestId('new-password').fill('a-good-password')
    await page.getByTestId('create-user').click()

    // Scope to the user list — the success toast also contains the name
    await expect(page.getByTestId('user-row').filter({ hasText: username })).toBeVisible()
  })

  test('lists scheduled jobs', async ({ page }) => {
    await page.getByTestId('nav-admin').click()
    await page.getByTestId('tab-jobs').click()
    // The E2E server runs with MUSICDROME_TESTING=true, so the scheduler is off
    // and the list is legitimately empty — the tab must still render.
    await expect(page.getByText(/Background tasks run inside the server/)).toBeVisible()
  })

  test('integration health lists each service', async ({ page }) => {
    await page.getByTestId('nav-admin').click()
    await page.getByTestId('tab-integrations').click()

    const rows = page.getByTestId('integration-row')
    await expect(rows.first()).toBeVisible()
    await expect(rows.filter({ hasText: 'Last.fm' })).toHaveCount(1)
    await expect(rows.filter({ hasText: 'Lidarr' })).toHaveCount(1)
    await expect(rows.filter({ hasText: 'ffmpeg' })).toHaveCount(1)
  })
})

test.describe('dark mode', () => {
  test('renders on a dark surface throughout', async ({ page }) => {
    // The <html> element carries the dark class and the body is near-black
    await expect(page.locator('html')).toHaveClass(/dark/)

    const background = await page.evaluate(() =>
      getComputedStyle(document.body).backgroundColor,
    )
    const [r, g, b] = background.match(/\d+/g)!.map(Number)
    expect(r + g + b).toBeLessThan(120) // very dark, not a light theme

    // Text is light against it, so contrast is the right way round
    const color = await page.evaluate(() => getComputedStyle(document.body).color)
    const [tr, tg, tb] = color.match(/\d+/g)!.map(Number)
    expect(tr + tg + tb).toBeGreaterThan(500)
  })

  test('the colour scheme is declared for form controls', async ({ page }) => {
    const scheme = await page.evaluate(() =>
      getComputedStyle(document.documentElement).colorScheme,
    )
    expect(scheme).toContain('dark')
  })
})

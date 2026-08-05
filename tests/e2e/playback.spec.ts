import { expect, test } from '@playwright/test'
import { gotoTab, isPlaying, login } from './helpers'

test.beforeEach(async ({ page }) => {
  await login(page)
})

test.describe('playback', () => {
  test('the player starts idle', async ({ page }) => {
    await expect(page.getByTestId('player')).toBeVisible()
    await expect(page.getByTestId('player-idle')).toBeVisible()
  })

  test('plays a track and reports progress', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Northern Lights').click()

    await page.getByTestId('track-row').first().getByTestId('play-track').click()

    await expect(page.getByTestId('now-playing-title')).toHaveText('First Light')
    await expect.poll(() => isPlaying(page), { timeout: 15_000 }).toBe(true)

    // The clock moves, which means bytes are actually flowing from /stream
    await expect
      .poll(
        async () => {
          const text = await page.getByTestId('position').textContent()
          const [minutes, seconds] = (text || '0:00').split(':').map(Number)
          return minutes * 60 + seconds
        },
        { timeout: 15_000 },
      )
      .toBeGreaterThan(0)
  })

  test('pause and resume work', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Northern Lights').click()
    await page.getByTestId('play-album').click()

    await expect.poll(() => isPlaying(page), { timeout: 15_000 }).toBe(true)

    await page.getByTestId('play-pause').click()
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            document.querySelector<HTMLAudioElement>('[data-testid="audio-element"]')?.paused ?? true,
        ),
      )
      .toBe(true)

    await page.getByTestId('play-pause').click()
    await expect.poll(() => isPlaying(page), { timeout: 10_000 }).toBe(true)
  })

  test('playing an album fills the queue and skip advances it', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Paper Trails').click()
    await page.getByTestId('play-album').click()

    await expect(page.getByTestId('now-playing-title')).toHaveText('Receipts')

    await page.getByTestId('queue-toggle').click()
    await expect(page.getByTestId('queue-panel')).toBeVisible()
    await expect(page.getByTestId('queue-panel').locator('li')).toHaveCount(5)

    await page.getByTestId('next').click()
    await expect(page.getByTestId('now-playing-title')).toHaveText('Small Print')

    await page.getByTestId('prev').click()
    await expect(page.getByTestId('now-playing-title')).toHaveText('Receipts')
  })

  test('the space bar toggles playback', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Northern Lights').click()
    await page.getByTestId('play-album').click()
    await expect.poll(() => isPlaying(page), { timeout: 15_000 }).toBe(true)

    await page.locator('body').press('Space')
    await expect
      .poll(() =>
        page.evaluate(
          () =>
            document.querySelector<HTMLAudioElement>('[data-testid="audio-element"]')?.paused ?? true,
        ),
      )
      .toBe(true)
  })

  test('shuffle and repeat toggle their pressed state', async ({ page }) => {
    const shuffle = page.getByTestId('shuffle')
    await expect(shuffle).toHaveAttribute('aria-pressed', 'false')
    await shuffle.click()
    await expect(shuffle).toHaveAttribute('aria-pressed', 'true')

    const repeat = page.getByTestId('repeat')
    await expect(repeat).toHaveAttribute('aria-label', 'Repeat: off')
    await repeat.click()
    await expect(repeat).toHaveAttribute('aria-label', 'Repeat: all')
    await repeat.click()
    await expect(repeat).toHaveAttribute('aria-label', 'Repeat: one')
  })

  test('a play is recorded in listening history', async ({ page, request }) => {
    const token = await page
      .request.post('/api/v1/auth/login', {
        data: {
          username: process.env.E2E_ADMIN_USERNAME || 'admin',
          password: process.env.E2E_ADMIN_PASSWORD || 'testadmin123',
        },
      })
      .then((r) => r.json())
      .then((body) => body.access_token)

    const before = await request
      .get('/api/v1/history', { headers: { Authorization: `Bearer ${token}` } })
      .then((r) => r.json())

    // The seeded tracks are 2s long, so half-way is reached quickly
    await gotoTab(page, 'albums')
    await page.getByText('Salt and Copper').click()
    await page.getByTestId('play-album').click()
    await page.waitForTimeout(4000)

    await expect
      .poll(
        async () => {
          const after = await request
            .get('/api/v1/history', { headers: { Authorization: `Bearer ${token}` } })
            .then((r) => r.json())
          return after.length
        },
        { timeout: 20_000 },
      )
      .toBeGreaterThan(before.length)
  })
})

test.describe('annotations', () => {
  test('starring a track persists across a reload', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Test Patterns').click()

    const row = page.getByTestId('track-row').first()
    await row.hover()
    await row.getByTestId('star-track').click()

    await page.reload()
    await expect(page.getByTestId('album-detail')).toBeVisible()
    // A starred track keeps the accent colour rather than only showing on hover
    await expect(
      page.getByTestId('track-row').first().getByTestId('star-track'),
    ).toHaveClass(/text-accent-soft/)
  })

  test('rating an album shows the chosen number of stars', async ({ page }) => {
    await gotoTab(page, 'albums')
    await page.getByText('Quiet Machines').click()

    const stars = page.getByTestId('album-detail').getByTestId('stars').first()
    await stars.getByLabel('4 stars').click()

    await page.reload()
    await expect(page.getByTestId('album-detail')).toBeVisible()
    const filled = page
      .getByTestId('album-detail')
      .getByTestId('stars')
      .first()
      .locator('button.text-amber-400')
    await expect(filled).toHaveCount(4)
  })
})

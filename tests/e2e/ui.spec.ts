import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';

/**
 * Browser smokes over the things the UI has to get right: cards render with
 * their match percentage, filters work, hiding one is durable, and a download
 * can be queued. Everything runs against a seeded database — no Last.fm key, no
 * AI backend, no network.
 *
 * The database is re-seeded before each test. These tests share one server
 * process, and hiding a card in one of them would otherwise change what the
 * next one sees.
 */

const dataDir = process.env.MUSICDROME_E2E_DIR!;
const python = process.env.PYTHON!;

test.beforeEach(() => {
  execFileSync(python, [join(__dirname, 'seed.py')], {
    env: {
      ...process.env,
      MUSICDROME_DATA_DIR: join(dataDir, 'config'),
      MUSICDROME_MUSIC_DIR: join(dataDir, 'music'),
    },
    stdio: 'ignore',
  });
});

test('the discover grid renders seeded cards, best match first', async ({ page }) => {
  await page.goto('/');

  const cards = page.locator('.card');
  await expect(cards).toHaveCount(4);

  await expect(cards.first()).toContainText('Roygbiv');
  await expect(cards.first()).toContainText('Boards of Canada');
  await expect(cards.first().locator('.match')).toHaveText('94%');

  // The reason line is what makes a recommendation legible.
  await expect(cards.first()).toContainText('Massive Attack');

  // Match tiers are colour-coded: 85+ high, 70+ mid, 50+ low, below that weak.
  await expect(cards.first().locator('.match')).toHaveClass(/tier-high/);
  await expect(cards.nth(2).locator('.match')).toHaveClass(/tier-mid/);
  await expect(cards.last().locator('.match')).toHaveClass(/tier-low/);
});

test('the setup banner names what is missing', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#banner')).toContainText('No listening history configured');
});

test('the minimum match filter drops weaker cards', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('.card')).toHaveCount(4);

  await page.locator('#f-match').fill('80');
  await page.locator('#f-match').dispatchEvent('change');

  await expect(page.locator('.card')).toHaveCount(2);
  await expect(page.locator('#f-match-value')).toHaveText('80%');
});

test('hiding a card removes it and it stays gone after a reload', async ({ page }) => {
  await page.goto('/');
  const first = page.locator('.card').first();
  await expect(first).toContainText('Roygbiv');

  await first.locator('[data-action="hide"]').click();
  await expect(page.locator('.card')).toHaveCount(3);
  await expect(page.locator('.card').first()).not.toContainText('Roygbiv');

  await page.reload();
  await expect(page.locator('.card')).toHaveCount(3);

  // It is still reachable under the hidden filter, not deleted.
  await page.locator('#f-status').selectOption('hidden');
  await expect(page.locator('.card')).toHaveCount(1);
  await expect(page.locator('.card').first()).toContainText('Roygbiv');
});

test('downloading a card queues it and it shows up on the downloads tab', async ({ page }) => {
  await page.goto('/');

  await page.locator('.card').first().locator('[data-action="download"]').click();
  await expect(page.locator('#toast')).toContainText('Queued for download');

  await page.locator('.tab[data-tab="downloads"]').click();
  const row = page.locator('#downloads-table tbody tr').first();
  await expect(row).toContainText('Roygbiv');
  await expect(row.locator('.pill')).toHaveText('queued');
});

test('the stats tab computes from the seeded plays', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="stats"]').click();

  await expect(page.locator('.tile').first()).toContainText('16');
  await expect(page.locator('#top-artists li').first()).toContainText('Radiohead');
  await expect(page.locator('#chart-clock .bar')).toHaveCount(24);

  // The bars must actually be drawn. A percentage height inside a shrink-wrapped
  // track resolves to nothing, which renders an empty chart rather than an error.
  const tallest = await page.locator('#chart-daily .bar > i').evaluateAll(
    (fills) => Math.max(...fills.map((fill) => fill.getBoundingClientRect().height)),
  );
  expect(tallest).toBeGreaterThan(10);
});

test('the paste box rejects a link it cannot use, and says why', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();

  await page.locator('#paste-url').fill('https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT');
  await page.locator('#paste-form button').click();

  await expect(page.locator('#toast')).toContainText('paste a track link');
  // The input keeps its value so the user can correct it rather than retype.
  await expect(page.locator('#paste-url')).toHaveValue(/spotify\.com/);
});

test('retry all failed is hidden until something has failed', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();
  await expect(page.locator('#retry-failed')).toBeHidden();
});

test('the scan progress bar appears only while a scan runs', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#scan-progress')).toBeHidden();

  // The seeded instance has no AI backend, so the scan fails fast — enough to
  // prove the bar is driven by scan state rather than always painted.
  await page.locator('#scan-now').click();
  await expect(page.locator('#scan-progress')).toBeVisible({ timeout: 5000 });
});

test('settings persist across a reload', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="settings"]').click();

  await page.locator('[data-setting="schedule"]').selectOption('weekly');
  await expect(page.locator('#toast')).toContainText('Saved');

  await page.reload();
  await page.locator('.tab[data-tab="settings"]').click();
  await expect(page.locator('[data-setting="schedule"]')).toHaveValue('weekly');
});

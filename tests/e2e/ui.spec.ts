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
  // toContainText, not toHaveText: a card the Navidrome hearts lifted carries a
  // heart glyph in the same pill. The assertion here is about the number.
  await expect(cards.first().locator('.match')).toContainText('94%');

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

/**
 * The downloads table is laid out `fixed` precisely so that one row cannot
 * decide how wide it is. The seed carries a DJ mix whose title and artist run
 * to ~130 characters each; under the browser default of `auto` that row alone
 * stretched the table to 2583px and pushed six of the eight columns off-screen
 * at every viewport size. Widths are asserted at four, because the columns are
 * folded away by media query as the window narrows and each band has to add up
 * on its own.
 */
for (const width of [1440, 1280, 1024, 900]) {
  test(`the downloads table fits a ${width}px window`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    await page.locator('.tab[data-tab="downloads"]').click();
    await expect(page.locator('#downloads-table tbody tr').first()).toBeVisible();

    const fit = await page.evaluate(() => {
      const frame = document.querySelector('.table-scroll')!;
      return {
        overflows: frame.scrollWidth > frame.clientWidth,
        page: document.documentElement.scrollWidth > window.innerWidth,
        // Nothing may be clipped horizontally inside a cell either, except the
        // values deliberately truncated with an ellipsis.
        tallest: Math.max(...[...document.querySelectorAll('#downloads-table tbody tr')]
          .map((row) => row.getBoundingClientRect().height)),
      };
    });

    expect(fit.overflows).toBe(false);
    expect(fit.page).toBe(false);
    // A row is two lines of title over one of artist, not a wall of text.
    expect(fit.tallest).toBeLessThan(130);
  });
}

test('the long title is clamped but still readable in full on hover', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();

  const title = page.locator('#downloads-table .t-title', { hasText: 'Tropical House' });
  await expect(title).toHaveAttribute('title', /Perfect Strangers$/);
  // Clamped to two lines: the full text needs more room than the cell gives it.
  expect(await title.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
});

test('the file column shows the filename and copies the path', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();

  const file = page.locator('#downloads-table tr', { hasText: 'Breathe' })
    .locator('.path').first();
  await expect(file).toHaveText('Breathe.opus');
  await expect(file).toHaveAttribute('title', '/music/Tinlicker/Breathe/Breathe.opus');
});

const SEEDED_DOWNLOADS = 8;

test('searching narrows the table without a request', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);

  await page.locator('#d-search').fill('tinlicker');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(1);
  await expect(page.locator('#downloads-table tbody tr')).toContainText('Breathe');

  // The path is searchable too, which is the only way to find a track by where
  // it landed.
  await page.locator('#d-search').fill('/Singles/');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(2);

  await page.locator('#d-search').fill('nothing at all matches this');
  await expect(page.locator('.table-scroll')).toBeHidden();
  await expect(page.locator('#downloads-empty')).toContainText('Nothing matches');

  await page.locator('#d-search').fill('');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);
});

test('a column heading sorts the table and says which way', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();

  const heading = page.locator('#downloads-table th[data-sort="size"]');
  const megabytes = async () =>
    (await page.locator('#downloads-table td[data-label="Size"]').allTextContents())
      .map(parseFloat);

  await heading.locator('.th-sort').click();
  await expect(heading).toHaveAttribute('aria-sort', 'descending');
  const descending = await megabytes();
  expect(descending).toHaveLength(SEEDED_DOWNLOADS);
  expect(descending).toEqual([...descending].sort((a, b) => b - a));

  // Clicking the same heading again reverses it rather than re-sorting.
  await heading.locator('.th-sort').click();
  await expect(heading).toHaveAttribute('aria-sort', 'ascending');
  expect(await megabytes()).toEqual(descending.slice().reverse());

  // A different heading takes over, and the old one stops claiming the sort.
  await page.locator('#downloads-table th[data-sort="track"] .th-sort').click();
  await expect(heading).toHaveAttribute('aria-sort', 'none');
  await expect(page.locator('#downloads-table th[data-sort="track"]'))
    .toHaveAttribute('aria-sort', 'ascending');
});

test('removing a download asks in the page, names the track, and can be cancelled',
  async ({ page }) => {
    await page.goto('/');
    await page.locator('.tab[data-tab="downloads"]').click();

    const row = page.locator('#downloads-table tr', { hasText: 'Breathe' }).first();
    await row.locator('[data-remove]').click();

    // An in-page dialog, not window.confirm(): it can name the track and ask
    // about the file on disk in the same breath.
    await expect(page.locator('#modal-title')).toHaveText('Remove this download?');
    await expect(page.locator('#modal-body')).toContainText('Tinlicker — Breathe');
    await expect(page.locator('#modal-option')).toBeVisible();
    await expect(page.locator('#modal-checkbox')).not.toBeChecked();

    await page.keyboard.press('Escape');
    await expect(page.locator('#modal')).toBeHidden();
    await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);

    await row.locator('[data-remove]').click();
    await page.locator('#modal-confirm').click();
    await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS - 1);
    await expect(page.locator('#toast')).toContainText('Removed from the list');
  });

/**
 * Selecting rows. The point of the feature is the case that cannot be tested
 * by hand: a hundred and seventy-eight downloads, one tick, one delete. What
 * these check is that the count can never lie about what Delete would take —
 * the search filters in the browser, so a selection outlives the rows that
 * made it.
 */

const picks = () => '#downloads-table tbody [data-pick]';

test('the selection bar stays out of the way until a row is ticked', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);

  await expect(page.locator('#d-selection')).toBeHidden();
  await expect(page.locator('#d-select-all')).not.toBeChecked();

  await page.locator(picks()).first().check();

  await expect(page.locator('#d-selection')).toBeVisible();
  await expect(page.locator('#d-selected-count')).toHaveText('1 selected');
  // Partial, so the header box reports the list rather than only what it would
  // do next.
  await expect(page.locator('#d-select-all')).toHaveJSProperty('indeterminate', true);

  await page.locator('#d-clear-selection').click();
  await expect(page.locator('#d-selection')).toBeHidden();
  await expect(page.locator('#d-select-all')).toHaveJSProperty('indeterminate', false);
});

test('select all takes everything the filter is showing, and no more', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();

  // Narrowed first: "everything shown" has to mean the search box too, not
  // just whatever the server last sent.
  await page.locator('#d-search').fill('/Singles/');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(2);

  await page.locator('#d-select-all').check();
  await expect(page.locator('#d-selected-count')).toHaveText('2 selected');

  // Widening the search must not quietly widen the selection with it.
  await page.locator('#d-search').fill('');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);
  await expect(page.locator('#d-selected-count')).toHaveText('2 selected');
  await expect(page.locator('#d-select-all')).toHaveJSProperty('indeterminate', true);

  await page.locator('#d-select-all').check();
  await expect(page.locator('#d-selected-count')).toHaveText(`${SEEDED_DOWNLOADS} selected`);
  await expect(page.locator('#d-select-all')).toBeChecked();

  await page.locator('#d-select-all').uncheck();
  await expect(page.locator('#d-selection')).toBeHidden();
});

test('a selection hidden by the search says so rather than counting silently',
  async ({ page }) => {
    await page.goto('/');
    await page.locator('.tab[data-tab="downloads"]').click();

    await page.locator('#d-select-all').check();
    await expect(page.locator('#d-selected-hidden')).toBeHidden();

    await page.locator('#d-search').fill('tinlicker');
    await expect(page.locator('#downloads-table tbody tr')).toHaveCount(1);

    // Still eight selected — and the bar has to say that seven of them are no
    // longer on the screen, or the count reads as "one".
    await expect(page.locator('#d-selected-count')).toHaveText(`${SEEDED_DOWNLOADS} selected`);
    await expect(page.locator('#d-selected-hidden'))
      .toContainText(`${SEEDED_DOWNLOADS - 1} not shown`);
  });

test('shift-clicking fills the range between two rows', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);

  await page.locator(picks()).nth(1).check();
  await page.locator(picks()).nth(4).click({ modifiers: ['Shift'] });

  await expect(page.locator('#d-selected-count')).toHaveText('4 selected');
  await expect(page.locator(picks()).nth(0)).not.toBeChecked();
  await expect(page.locator(picks()).nth(2)).toBeChecked();
  await expect(page.locator(picks()).nth(5)).not.toBeChecked();
});

test('changing the status filter drops a selection made against the old rows',
  async ({ page }) => {
    await page.goto('/');
    await page.locator('.tab[data-tab="downloads"]').click();

    await page.locator('#d-select-all').check();
    await expect(page.locator('#d-selection')).toBeVisible();

    await page.locator('#d-status').selectOption('failed');
    await expect(page.locator('#d-selection')).toBeHidden();
  });

test('deleting a selection takes those rows and leaves the rest', async ({ page }) => {
  await page.goto('/');
  await page.locator('.tab[data-tab="downloads"]').click();
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);

  await page.locator('#d-search').fill('/Singles/');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(2);
  await page.locator('#d-select-all').check();
  await page.locator('#d-search').fill('');

  await page.locator('#d-delete-selected').click();

  // The same in-page dialog the single delete uses, with no option to keep the
  // files: a bulk delete always takes them.
  await expect(page.locator('#modal-title')).toHaveText('Delete 2 downloads?');
  await expect(page.locator('#modal-body')).toContainText('suggestable again');
  await expect(page.locator('#modal-option')).toBeHidden();

  await page.keyboard.press('Escape');
  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS);
  await expect(page.locator('#d-selected-count')).toHaveText('2 selected');

  await page.locator('#d-delete-selected').click();
  await page.locator('#modal-confirm').click();

  await expect(page.locator('#downloads-table tbody tr')).toHaveCount(SEEDED_DOWNLOADS - 2);
  await expect(page.locator('#toast')).toContainText('Removed 2');
  await expect(page.locator('#d-selection')).toBeHidden();
  // The two that went are the two that were ticked.
  await expect(page.locator('#downloads-table tbody')).not.toContainText('Moments');
  await expect(page.locator('#downloads-table tbody')).not.toContainText('Heartbeat');
  await expect(page.locator('#downloads-table tbody')).toContainText('Breathe');
});

test('the column headings pin under the top bar when the list is scrolled',
  async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 500 });
    await page.goto('/');
    await page.locator('.tab[data-tab="downloads"]').click();
    await expect(page.locator('#downloads-table tbody tr').first()).toBeVisible();

    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.waitForTimeout(300);

    const pinned = await page.evaluate(() => {
      const heading = document.querySelector('#downloads-table thead th')!;
      const bar = document.querySelector('.topbar')!;
      return {
        scrolled: window.scrollY,
        heading: heading.getBoundingClientRect().top,
        bar: bar.getBoundingClientRect().bottom,
      };
    });

    // The page has to have moved far enough for the question to mean anything.
    expect(pinned.scrolled).toBeGreaterThan(200);
    // Flush against the bar: still on screen, and not hidden behind it.
    expect(Math.abs(pinned.heading - pinned.bar)).toBeLessThan(2);
  });

test('the scan progress bar appears only while a scan runs', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('#scan-progress')).toBeHidden();

  // The seeded instance has no AI backend, so the scan fails fast — enough to
  // prove the bar is driven by scan state rather than always painted.
  await page.locator('#scan-now').click();
  await expect(page.locator('#scan-progress')).toBeVisible({ timeout: 5000 });
});

test('a match the Navidrome hearts lifted says so, and says by how much', async ({ page }) => {
  await page.goto('/');

  // Boosted: 82 from the model, +12 from the hearts.
  const boosted = page.locator('.card', { hasText: 'Roygbiv' }).locator('.match');
  await expect(boosted).toHaveClass(/is-hearted/);
  await expect(boosted).toContainText('94%');
  await expect(boosted).toHaveAttribute(
    'title',
    '82% from the model, +12 from what you heart — you have hearted 3 tracks by Boards of Canada',
  );

  // Unboosted cards must look exactly as they always did.
  const plain = page.locator('.card', { hasText: 'Fade Into You' }).locator('.match');
  await expect(plain).not.toHaveClass(/is-hearted/);
  await expect(plain).toHaveAttribute('title', '88% match');
});

test('the connections panel reports Navidrome', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('tab', { name: 'Settings' }).click();

  const row = page.locator('#connections div', { hasText: 'Navidrome' });
  await expect(row).toContainText('3 hearted of 4');
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

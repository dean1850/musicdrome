import { defineConfig, devices } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const repoRoot = join(__dirname, '..', '..');
const python = process.env.PYTHON ?? join(repoRoot, '.venv', 'bin', 'python');
const port = process.env.PORT ?? '3047';

// A throwaway data directory, re-seeded before every test. The path is put back
// into the environment so the spec files inherit it and can re-run the seed
// against the same database the server is using.
const dataDir = process.env.MUSICDROME_E2E_DIR ?? join(tmpdir(), 'musicdrome-e2e');
process.env.MUSICDROME_E2E_DIR = dataDir;
process.env.PYTHON = python;

// CI images often ship their own Chromium rather than the build this Playwright
// version would download. Point CHROMIUM_PATH at it to use it as-is.
const executablePath = process.env.CHROMIUM_PATH || undefined;

// MUSICDROME_TESTING keeps the download workers and the scan scheduler off, so
// the browser tests are deterministic and make no outbound requests.
const serverEnv = {
  MUSICDROME_TESTING: '1',
  MUSICDROME_DATA_DIR: join(dataDir, 'config'),
  MUSICDROME_MUSIC_DIR: join(dataDir, 'music'),
  MUSICDROME_PORT: port,
  LASTFM_API_KEY: '',
  LASTFM_USER: '',
  LISTENBRAINZ_USER: '',
  EXCLUDE_MUSIC_DIR: '',
  TZ: 'UTC',
};

export default defineConfig({
  testDir: '.',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    colorScheme: 'dark',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], launchOptions: { executablePath } } },
  ],
  webServer: {
    command:
      `${python} tests/e2e/seed.py && ` +
      `${python} -m uvicorn app.main:app --host 127.0.0.1 --port ${port}`,
    cwd: repoRoot,
    url: `http://127.0.0.1:${port}/api/health`,
    timeout: 60_000,
    reuseExistingServer: false,
    env: serverEnv,
  },
});

import { existsSync } from 'node:fs'
import { defineConfig, devices } from '@playwright/test'

/**
 * Set E2E_BASE_URL to test an already-running server — a `docker compose up`
 * stack, or a dev server — and Playwright talks to it directly. Leave it unset
 * and Playwright boots a throwaway instance itself via run-test-server.sh.
 */
const externalServer = Boolean(process.env.E2E_BASE_URL)
const port = process.env.E2E_PORT || '4599'
const baseURL = process.env.E2E_BASE_URL || `http://127.0.0.1:${port}`

/**
 * Some images ship a Chromium build that doesn't match what this Playwright
 * version would download. Point at the provided binary when one is present
 * rather than failing with "Executable doesn't exist".
 */
const providedChromium =
  process.env.PLAYWRIGHT_CHROMIUM_PATH ||
  ['/opt/pw-browsers/chromium', '/usr/bin/chromium', '/usr/bin/chromium-browser'].find((path) =>
    existsSync(path),
  )

export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // Serial: the suite mutates one shared library and one shared account.
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]]
    : [['list']],

  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 15_000,
    // Autoplay policies otherwise block the <audio> element in headless runs
    launchOptions: {
      args: ['--autoplay-policy=no-user-gesture-required'],
      ...(providedChromium ? { executablePath: providedChromium } : {}),
    },
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1400, height: 900 },
        // The bundled binary is full Chromium, not the headless shell build
        ...(providedChromium ? { channel: undefined } : {}),
      },
    },
  ],

  webServer: externalServer
    ? undefined
    : {
        command: 'bash ./run-test-server.sh',
        url: `http://127.0.0.1:${port}/api/v1/health`,
        reuseExistingServer: !process.env.CI,
        timeout: 180_000,
        stdout: 'pipe',
        stderr: 'pipe',
      },
})

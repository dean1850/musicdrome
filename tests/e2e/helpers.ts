import { expect, type Page } from '@playwright/test'

export const ADMIN = {
  username: process.env.E2E_ADMIN_USERNAME || 'admin',
  password: process.env.E2E_ADMIN_PASSWORD || 'testadmin123',
}

/** Sign in through the UI and wait for the app shell to render. */
export async function login(page: Page, username = ADMIN.username, password = ADMIN.password) {
  await page.goto('/')
  await page.getByTestId('login-form').waitFor()
  await page.getByTestId('username').fill(username)
  await page.getByTestId('password').fill(password)
  await page.getByTestId('submit').click()
  await expect(page.getByTestId('sidebar')).toBeVisible({ timeout: 20_000 })
}

/** Grab a bearer token straight from the API, for direct request-level checks. */
export async function apiToken(page: Page): Promise<string> {
  const response = await page.request.post('/api/v1/auth/login', {
    data: { username: ADMIN.username, password: ADMIN.password },
  })
  expect(response.ok()).toBeTruthy()
  return (await response.json()).access_token
}

export async function gotoTab(page: Page, name: string) {
  await page.getByTestId(`nav-${name}`).click()
}

/** True once the <audio> element is actually producing sound. */
export async function isPlaying(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const audio = document.querySelector<HTMLAudioElement>('[data-testid="audio-element"]')
    return !!audio && !audio.paused && audio.currentTime > 0
  })
}

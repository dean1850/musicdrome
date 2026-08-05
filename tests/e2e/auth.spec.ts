import { expect, test } from '@playwright/test'
import { ADMIN, login } from './helpers'

test.describe('authentication', () => {
  test('shows the login screen when signed out', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByTestId('login-form')).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Musicdrome' })).toBeVisible()
    await expect(page.getByTestId('sidebar')).toHaveCount(0)
  })

  test('rejects a wrong password without signing in', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('username').fill(ADMIN.username)
    await page.getByTestId('password').fill('definitely-not-the-password')
    await page.getByTestId('submit').click()

    await expect(page.getByTestId('error-banner')).toBeVisible()
    await expect(page.getByTestId('login-form')).toBeVisible()
  })

  test('signs in and lands on the library', async ({ page }) => {
    await login(page)
    await expect(page.getByTestId('current-user')).toHaveText(ADMIN.username)
    await expect(page.getByTestId('home-page')).toBeVisible()
  })

  test('keeps the session across a reload', async ({ page }) => {
    await login(page)
    await page.reload()
    await expect(page.getByTestId('sidebar')).toBeVisible()
    await expect(page.getByTestId('current-user')).toHaveText(ADMIN.username)
  })

  test('signs out back to the login screen', async ({ page }) => {
    await login(page)
    await page.getByTestId('logout').click()
    await expect(page.getByTestId('login-form')).toBeVisible()

    // And the session really is gone, not just visually reset
    await page.reload()
    await expect(page.getByTestId('login-form')).toBeVisible()
  })

  test('admin-only navigation is present for an administrator', async ({ page }) => {
    await login(page)
    await expect(page.getByTestId('nav-admin')).toBeVisible()
  })

  test('a non-admin user cannot see the admin area', async ({ page }) => {
    // Create the account through the API, then sign in as them in the UI
    const token = await page
      .request.post('/api/v1/auth/login', { data: ADMIN })
      .then((r) => r.json())
      .then((body) => body.access_token)

    const username = `listener_${Date.now()}`
    const created = await page.request.post('/api/v1/admin/users', {
      headers: { Authorization: `Bearer ${token}` },
      data: { username, password: 'listener-password', is_admin: false },
    })
    expect(created.ok()).toBeTruthy()

    await login(page, username, 'listener-password')
    await expect(page.getByTestId('current-user')).toHaveText(username)
    await expect(page.getByTestId('nav-admin')).toHaveCount(0)

    // Direct navigation is redirected rather than rendering the admin page
    await page.goto('/admin')
    await expect(page.getByTestId('admin-page')).toHaveCount(0)
  })
})

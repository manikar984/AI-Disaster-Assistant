// Full-stack integration smoke test.
// Requires a built frontend served by the FastAPI application on a disposable
// database (see .github/scripts/browser-smoke.sh). External feeds stay disabled.
import assert from 'node:assert/strict';
import { launchBrowser, instrumentPage, reportFailure, checkpoint, layoutSnapshot, waitForRowCount } from './browser-helpers.mjs';

const { browser, channel } = await launchBrowser();
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
const evidence = instrumentPage(page);
try {
  checkpoint('open dashboard');
  await page.goto(process.env.TEST_URL || 'http://127.0.0.1:8001');
  await page.getByRole('heading', { name: 'A clearer view. A safer tomorrow.' }).waitFor();
  await page.getByRole('heading', { name: 'No live incidents in this view' }).waitFor();
  checkpoint('open sample mode');
  await page.getByRole('button', { name: 'Sample data', exact: true }).click();
  await page.getByRole('button', { name: /Flood sample Kerala/ }).click();
  await page.getByRole('heading', { name: 'Supporting evidence' }).waitFor();
  await page.getByRole('heading', { name: 'Flood · Kerala' }).waitFor();
  await page.keyboard.press('Escape');
  checkpoint('search filter');
  await page.getByRole('textbox', { name: 'Search locations or incidents' }).fill('Japan');
  await page.getByRole('button', { name: /Earthquake sample Japan/ }).waitFor();
  await waitForRowCount(page, 1);
  assert.match(await page.locator('.incident-row').innerText(), /Japan/, 'The filtered row belongs to the searched location');
  await page.getByRole('button', { name: 'Clear search' }).click();
  assert.equal(await page.locator('.leaflet-container').count(), 1);
  checkpoint('registration');
  await page.getByRole('button', { name: 'Sign in', exact: true }).first().click();
  await page.getByRole('button', { name: 'Create an account', exact: true }).click();
  const account = `browser-${Date.now()}@example.com`;
  await page.getByLabel('Email address').fill(account);
  await page.getByLabel('Password', { exact: true }).fill('Browser-test-password-47');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  // Registration answers identically for a new and an existing address and no
  // longer starts a session, so the dialog hands the user back to sign in. The
  // account it just created is real: signing in with that password must work.
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('heading', { name: 'Welcome back' }).waitFor();
  await dialog.getByLabel('Email address').fill(account);
  await dialog.getByLabel('Password', { exact: true }).fill('Browser-test-password-47');
  await dialog.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('button', { name: 'My account', exact: true }).waitFor();
  // A new account starts unconfirmed: the dashboard must say so and offer the resend.
  await page.getByRole('status').filter({ hasText: 'Confirm your email address' }).waitFor();
  assert.equal(
    await page.getByRole('button', { name: 'Resend link', exact: true }).count(),
    1,
    'An unconfirmed account can request the confirmation link again',
  );
  checkpoint('password recovery entry point');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('button', { name: 'Sign in', exact: true }).first().click();
  await page.getByRole('button', { name: 'Send a recovery link', exact: true }).click();
  await page.getByRole('heading', { name: 'Reset your password' }).waitFor();
  await page.getByLabel('Email address').fill(`nobody-${Date.now()}@example.com`);
  await page.getByRole('button', { name: 'Send recovery link', exact: true }).click();
  await page.getByRole('status').filter({ hasText: 'If that address has an account' }).waitFor();
  // The answer is identical for an address with no account: no enumeration.
  await page.getByRole('button', { name: 'Sign in', exact: true }).first().click();
  await page.getByLabel('Email address').fill(account);
  await page.getByLabel('Password', { exact: true }).fill('Browser-test-password-47');
  await page.getByRole('button', { name: 'Sign in', exact: true }).last().click();
  await page.getByRole('button', { name: 'My account', exact: true }).waitFor();
  await page.getByRole('button', { name: 'My account', exact: true }).click();
  await page.getByRole('button', { name: 'Add location', exact: true }).click();
  await page.getByLabel('Location name').fill('Browser test home');
  await page.getByLabel('Latitude', { exact: true }).fill('10.8505');
  await page.getByLabel('Longitude', { exact: true }).fill('76.2711');
  await page.getByRole('button', { name: 'Save location', exact: true }).click();
  await page.getByRole('heading', { name: 'Browser test home' }).waitFor();
  checkpoint('community report');
  await page.getByRole('button', { name: 'Submit a report', exact: true }).click();
  await page.getByLabel('Location', { exact: true }).fill('Browser test district');
  await page.getByLabel('What happened?').fill('This is a simulation of a flood report for browser testing.');
  await page.getByRole('button', { name: 'Submit for review', exact: true }).click();
  await page.getByRole('status').filter({ hasText: 'Report recorded for review' }).waitFor();
  await page.getByRole('button', { name: 'Remove location', exact: true }).click();
  await page.getByRole('heading', { name: 'No locations saved yet' }).waitFor();
  checkpoint('account deletion');
  await page.getByRole('button', { name: 'Delete account', exact: true }).click();
  await page.getByRole('button', { name: 'Permanently delete account' }).click();
  await page.getByRole('heading', { name: 'A clearer view. A safer tomorrow.' }).waitFor();
  checkpoint('mobile navigation');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open menu' }).click();
  await page.getByRole('button', { name: 'Data sources', exact: true }).click();
  await page.getByRole('heading', { name: 'Transparent by design' }).waitFor();
  const layout = await layoutSnapshot(page);
  assert.equal(layout.scroll_width <= layout.viewport_width, true, `Mobile horizontal overflow: ${JSON.stringify(layout)}`);
  assert.deepEqual(evidence.pageErrors, [], 'No browser exceptions');
  console.log(`PASS (${channel}): desktop feed, map, filters, evidence, authentication, saved location, public report, account deletion, mobile navigation, no horizontal overflow, no browser exceptions.`);
} catch (error) {
  await reportFailure({ channel }, page, evidence, error, { layout: JSON.stringify(await layoutSnapshot(page)) });
  throw error;
} finally {
  await browser.close();
}

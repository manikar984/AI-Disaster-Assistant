// Full-stack integration smoke test.
// Requires a built frontend served by the FastAPI application on a disposable
// database (see .github/scripts/browser-smoke.sh). External feeds stay disabled.
import assert from 'node:assert/strict';
import { launchBrowser, instrumentPage, reportFailure } from './browser-helpers.mjs';

const { browser, channel } = await launchBrowser();
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
const evidence = instrumentPage(page);
try {
  await page.goto(process.env.TEST_URL || 'http://127.0.0.1:8001');
  await page.getByRole('heading', { name: 'A clearer view. A safer tomorrow.' }).waitFor();
  await page.getByRole('heading', { name: 'No live incidents in this view' }).waitFor();
  await page.getByRole('button', { name: 'Sample data', exact: true }).click();
  await page.getByRole('button', { name: /Flood sample Kerala/ }).click();
  await page.getByRole('heading', { name: 'Supporting evidence' }).waitFor();
  await page.getByRole('heading', { name: 'Flood · Kerala' }).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('textbox', { name: 'Search locations or incidents' }).fill('Japan');
  await page.getByRole('button', { name: /Earthquake sample Japan/ }).waitFor();
  assert.equal(await page.locator('.incident-row').count(), 1);
  await page.getByRole('button', { name: 'Clear search' }).click();
  assert.equal(await page.locator('.leaflet-container').count(), 1);
  await page.getByRole('button', { name: 'Sign in', exact: true }).first().click();
  await page.getByRole('button', { name: 'Create an account', exact: true }).click();
  await page.getByLabel('Email address').fill(`browser-${Date.now()}@example.com`);
  await page.getByLabel('Password', { exact: true }).fill('Browser-test-password-47');
  await page.getByRole('button', { name: 'Create account', exact: true }).click();
  await page.getByRole('button', { name: 'My account', exact: true }).waitFor();
  await page.getByRole('button', { name: 'My account', exact: true }).click();
  await page.getByRole('button', { name: 'Add location', exact: true }).click();
  await page.getByLabel('Location name').fill('Browser test home');
  await page.getByLabel('Latitude', { exact: true }).fill('10.8505');
  await page.getByLabel('Longitude', { exact: true }).fill('76.2711');
  await page.getByRole('button', { name: 'Save location', exact: true }).click();
  await page.getByRole('heading', { name: 'Browser test home' }).waitFor();
  await page.getByRole('button', { name: 'Submit a report', exact: true }).click();
  await page.getByLabel('Location', { exact: true }).fill('Browser test district');
  await page.getByLabel('What happened?').fill('This is a simulation of a flood report for browser testing.');
  await page.getByRole('button', { name: 'Submit for review', exact: true }).click();
  await page.getByRole('status').filter({ hasText: 'Report recorded for review' }).waitFor();
  await page.getByRole('button', { name: 'Remove location', exact: true }).click();
  await page.getByRole('heading', { name: 'No locations saved yet' }).waitFor();
  await page.getByRole('button', { name: 'Delete account', exact: true }).click();
  await page.getByRole('button', { name: 'Permanently delete account' }).click();
  await page.getByRole('heading', { name: 'A clearer view. A safer tomorrow.' }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open menu' }).click();
  await page.getByRole('button', { name: 'Data sources', exact: true }).click();
  await page.getByRole('heading', { name: 'Transparent by design' }).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'Mobile horizontal overflow');
  assert.deepEqual(evidence.pageErrors, [], 'No browser exceptions');
  console.log(`PASS (${channel}): desktop feed, map, filters, evidence, authentication, saved location, public report, account deletion, mobile navigation, no horizontal overflow, no browser exceptions.`);
} catch (error) {
  await reportFailure({ channel }, page, evidence, error);
  throw error;
} finally {
  await browser.close();
}

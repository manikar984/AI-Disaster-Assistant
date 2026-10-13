// Static-preview smoke test.
//
// Reproduces the GitHub Pages deployment: the built frontend is served as static
// files with no API at all (asset requests resolve, /api/* returns a 404 HTML
// page like GitHub Pages does). The preview must state plainly that no backend
// is connected instead of implying that no incidents exist or showing stale
// "live" data. Start it with: npm run test:static --prefix web
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { launchBrowser, instrumentPage, reportFailure } from './browser-helpers.mjs';

const root = new URL('../dist/', import.meta.url).pathname;
const port = Number(process.env.STATIC_PORT || 8022);
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2' };

const server = createServer(async (request, response) => {
  const path = decodeURIComponent(new URL(request.url, 'http://localhost').pathname);
  const target = join(root, normalize(path).replace(/^(\.\.[/\\])+/, ''));
  try {
    const info = await stat(target).catch(() => null);
    const file = info?.isFile() ? target : join(root, 'index.html');
    const body = await readFile(file);
    response.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream' });
    response.end(body);
  } catch {
    // GitHub Pages answers unknown paths with an HTML 404 page, not JSON.
    response.writeHead(404, { 'Content-Type': 'text/html' });
    response.end('<!doctype html><title>404</title><h1>404</h1>');
  }
});
await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));

const { browser, channel } = await launchBrowser();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const evidence = instrumentPage(page);
try {
  await page.goto(`http://127.0.0.1:${port}/`);
  await page.getByRole('heading', { name: 'A clearer view. A safer tomorrow.' }).waitFor();
  // The dashboard must never present an unreachable backend as a live all-clear.
  await page.locator('.demo-banner').filter({ hasText: 'No backend is connected at this address' }).waitFor();
  await page.getByRole('heading', { name: 'Backend not connected' }).waitFor();
  assert.equal(await page.locator('.incident-row').count(), 0, 'Static preview must not render incidents');
  assert.equal(/0 incidents|Monitored incidents/.test(await page.locator('.stats-grid').innerText()), true, 'Overview statistics remain visible');
  assert.equal(await page.locator('.stats-grid').innerText().then((text) => text.includes('—')), true, 'Counts stay unknown without a backend');
  // API-dependent actions explain themselves rather than failing silently.
  await page.getByRole('button', { name: 'Sign in', exact: true }).first().click();
  await page.locator('.toast').filter({ hasText: 'unavailable' }).waitFor();
  assert.equal(await page.locator('[role="dialog"]').count(), 0, 'No sign-in dialog without a backend');
  // The service health panel must not claim a healthy live source.
  assert.deepEqual(evidence.pageErrors, [], 'No browser exceptions');
  console.log(`PASS (${channel}): static preview discloses the missing backend, shows no incidents, blocks misleading actions, no browser exceptions.`);
} catch (error) {
  await reportFailure({ channel }, page, evidence, error);
  throw error;
} finally {
  await browser.close();
  server.close();
}

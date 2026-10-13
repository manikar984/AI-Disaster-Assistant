// Shared browser launcher and failure diagnostics for the integration smoke tests.
//
// The CI runner has Google Chrome installed, this restricted development sandbox
// does not; some containers lack the shared libraries that Playwright expects.
// The launcher therefore tries several documented browsers in order and reports
// exactly what failed instead of dying with an opaque timeout.
import { chromium as playwright } from '@playwright/test';
import chromium from '@sparticuz/chromium';
import { brotliDecompressSync } from 'node:zlib';
import { readFileSync, mkdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { tmpdir } from 'node:os';

// Chromium flags that would hide genuine same-origin/security failures.
const REJECTED_ARG_PARTS = ['disable-web-security', 'allow-running-insecure-content', 'ignore-certificate-errors'];
const SAFE_EXTRA_ARGS = ['--no-sandbox', '--disable-dev-shm-usage'];

export function bundledArgs() {
  return [...chromium.args.filter((arg) => !REJECTED_ARG_PARTS.some((part) => arg.includes(part))), ...SAFE_EXTRA_ARGS];
}

let libsPrepared = false;

// Amazon Linux (Lambda) runtime libraries shipped with @sparticuz/chromium.
// Extraction failing is not fatal: many hosts already provide these libraries.
function prepareRuntimeLibs() {
  if (process.platform !== 'linux' || libsPrepared) return;
  libsPrepared = true;
  try {
    const root = dirname(dirname(createRequire(import.meta.url).resolve('@sparticuz/chromium')));
    const libs = join(process.env.RUNNER_TEMP || tmpdir(), 'disaster-chromium-libs');
    mkdirSync(libs, { recursive: true });
    execFileSync('tar', ['-x', '-C', libs], {
      input: brotliDecompressSync(readFileSync(join(root, 'bin', 'al2023.tar.br'))),
      stdio: ['pipe', 'ignore', 'pipe']
    });
    process.env.LD_LIBRARY_PATH = join(libs, 'lib') + (process.env.LD_LIBRARY_PATH ? ':' + process.env.LD_LIBRARY_PATH : '');
  } catch (error) {
    console.error('[smoke] chromium runtime library extraction skipped:', error.message);
  }
}

/**
 * Launch a browser. `TEST_BROWSER=chrome` forces a system Chrome,
 * `TEST_BROWSER=bundled` forces the bundled headless Chromium,
 * otherwise both are attempted (bundled first) and every failure is reported.
 */
export async function launchBrowser() {
  const requested = (process.env.TEST_BROWSER || 'auto').toLowerCase();
  const attempts = [];
  if (requested !== 'chrome') {
    attempts.push(['bundled-chromium', async () => {
      prepareRuntimeLibs();
      return playwright.launch({
        args: bundledArgs(),
        executablePath: await chromium.executablePath(),
        headless: true
      });
    }]);
  }
  if (requested !== 'bundled') {
    attempts.push(['system-chrome', async () => playwright.launch({ channel: 'chrome', headless: true, args: SAFE_EXTRA_ARGS })]);
    attempts.push(['playwright-chromium', async () => playwright.launch({ headless: true, args: SAFE_EXTRA_ARGS })]);
  }
  const failures = [];
  for (const [name, start] of attempts) {
    try {
      const browser = await start();
      return { browser, channel: name };
    } catch (error) {
      failures.push(`${name}: ${String(error.message).split('\n')[0]}`);
      console.error(`[smoke] browser attempt failed — ${name}: ${String(error.message).split('\n')[0]}`);
    }
  }
  throw new Error(
    `No usable browser for the smoke test (TEST_BROWSER=${requested}).\n` +
      failures.map((failure) => `  - ${failure}`).join('\n') +
      '\n  Install a system Chrome/Chromium or run: npx playwright install --with-deps chromium'
  );
}

/** Collect browser-side evidence so a failure is diagnosable from logs alone. */
export function instrumentPage(page) {
  const evidence = { pageErrors: [], consoleErrors: [], failedRequests: [] };
  page.on('pageerror', (error) => evidence.pageErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') evidence.consoleErrors.push(`${message.text()} @ ${message.location().url}`);
  });
  page.on('requestfailed', (request) => evidence.failedRequests.push(`${request.method()} ${request.url()} — ${request.failure()?.errorText}`));
  return evidence;
}

export async function reportFailure(context, page, evidence, error) {
  const lines = [
    '--- SMOKE TEST FAILURE DIAGNOSTICS ---',
    `browser channel : ${context.channel}`,
    `node            : ${process.version} on ${process.platform}/${process.arch}`,
    `TEST_URL        : ${process.env.TEST_URL || '(default)'}`,
    `page url        : ${page ? page.url() : '(no page)'}`,
    `error           : ${error && error.message ? error.message.split('\n')[0] : error}`,
    `page errors     : ${evidence.pageErrors.length}`,
    `console errors  : ${evidence.consoleErrors.length}`,
    `failed requests : ${evidence.failedRequests.length}`
  ];
  for (const [label, items] of [
    ['page error', evidence.pageErrors],
    ['console error', evidence.consoleErrors],
    ['failed request', evidence.failedRequests]
  ]) {
    for (const item of items.slice(-6)) lines.push(`  ${label}: ${item}`);
  }
  if (page) {
    try {
      mkdirSync('test-results', { recursive: true });
      await page.screenshot({ path: join('test-results', 'smoke-failure.png'), fullPage: true });
      lines.push('screenshot      : test-results/smoke-failure.png');
      lines.push(`body text       : ${(await page.innerText('body')).replace(/\s+/g, ' ').slice(0, 400)}`);
    } catch (captureError) {
      lines.push(`screenshot      : unavailable (${captureError.message.split('\n')[0]})`);
    }
  }
  lines.push('--- END DIAGNOSTICS ---');
  console.error(lines.join('\n'));
}

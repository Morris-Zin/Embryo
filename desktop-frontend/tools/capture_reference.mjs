#!/usr/bin/env node
/* Deterministic web reference capture for migration verification.
 * Captures screenshots, DOM control geometry, text, keyboard-relevant metadata and network/API traces.
 */
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import { existsSync } from 'node:fs';

const require = createRequire(new URL('../../perf-suite/browser/package.json', import.meta.url));
const { chromium } = require('@playwright/test');

function arg(name, fallback = '') {
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 ? process.argv[index + 1] : fallback;
}

const baseUrl = arg('base-url', 'http://127.0.0.1:18086').replace(/\/$/, '');
const outDir = path.resolve(arg('out', 'desktop-frontend/evidence/web-reference'));
const routes = arg('routes', '/khitmee_sales,/khitmee_transfer').split(',').map((route) => route.trim()).filter(Boolean);
const username = arg('username', process.env.ERPNEXT_USERNAME || 'Administrator');
const password = arg('password', process.env.ERPNEXT_PASSWORD || process.env.ADMIN_PASSWORD || '');
const headless = arg('headed', 'false') !== 'true';
const stateName = arg('state', 'initial');
const actions = JSON.parse(arg('actions', '[]'));
if (!/^[a-z0-9][a-z0-9-]*$/.test(stateName)) throw new Error('state must be a lowercase evidence directory name');

async function sha256File(file) {
  return crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex');
}

async function main() {
  await fs.mkdir(outDir, { recursive: true });
  const launchOptions = { headless };
  const devinChrome = 'C:/devin/chrome/chrome-win64/chrome.exe';
  if (process.platform === 'win32' && existsSync(devinChrome)) launchOptions.executablePath = devinChrome;
  else if (process.platform === 'win32') launchOptions.channel = 'chrome';
  const browser = await chromium.launch(launchOptions);
  const context = await browser.newContext({
    viewport: { width: 1180, height: 760 },
    deviceScaleFactor: 1,
    locale: 'en-US',
    timezoneId: 'UTC',
    reducedMotion: 'reduce',
    colorScheme: 'light',
  });
  const page = await context.newPage();
  const trace = [];
  const requests = [];
  page.on('console', (message) => trace.push({ type: 'console', level: message.type(), text: message.text() }));
  page.on('pageerror', (error) => trace.push({ type: 'pageerror', text: String(error) }));
  page.on('requestfinished', async (request) => {
    const response = await request.response();
    requests.push({
      url: request.url(),
      method: request.method(),
      resourceType: request.resourceType(),
      status: response?.status() ?? null,
      postData: request.postData()?.slice(0, 500) ?? null,
    });
  });
  page.on('requestfailed', (request) => {
    requests.push({ url: request.url(), method: request.method(), failure: request.failure()?.errorText ?? 'failed' });
  });

  if (password) {
    await page.goto(`${baseUrl}/login`, { waitUntil: 'domcontentloaded' });
    await page.fill('#login_email', username);
    await page.fill('#login_password', password);
    await Promise.all([
      page.waitForLoadState('networkidle').catch(() => {}),
      page.click('.btn-login, button[type="submit"]'),
    ]);
  }

  const manifest = {
    schemaVersion: 1,
    capturedAt: new Date().toISOString(),
    baseUrl,
    viewport: { width: 1180, height: 760, deviceScaleFactor: 1 },
    routes: [],
    authentication: password ? 'credentials supplied' : 'no credentials supplied',
  };

  for (const route of routes) {
    const id = route.replace(/^\//, '').replaceAll('/', '-').replaceAll('_', '-').replaceAll('#', '--') || 'root';
    const screenDir = stateName === 'initial' ? path.join(outDir, id) : path.join(outDir, id, stateName);
    await fs.mkdir(screenDir, { recursive: true });
    requests.length = 0;
    trace.length = 0;
    const response = await page.goto(`${baseUrl}${route}`, { waitUntil: 'domcontentloaded' });
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(500);
    for (const [index, action] of actions.entries()) {
      const target = action.selector ? page.locator(action.selector).first() : null;
      trace.push({ type: 'capture-action', index, action });
      if (action.type === 'fill') await target.fill(String(action.value ?? ''));
      else if (action.type === 'click-if-exists') { if (await target.count() && await target.isVisible().catch(() => false)) await target.click(); }
      else if (action.type === 'click') await target.click();
      else if (action.type === 'press') await target.press(action.key);
      else if (action.type === 'key') await page.keyboard.press(action.key);
      else if (action.type === 'wait') await page.waitForTimeout(Number(action.ms || 0));
      else if (action.type === 'select') await target.selectOption(String(action.value ?? ''));
      else throw new Error(`unsupported capture action type: ${action.type}`);
      await page.waitForTimeout(Number(action.settleMs ?? 150));
    }
    await page.waitForTimeout(250);

    const screenshotPath = path.join(screenDir, 'reference.png');
    await page.screenshot({ path: screenshotPath, fullPage: false, animations: 'disabled' });

    const dom = await page.evaluate(() => {
      const controls = [...document.querySelectorAll('input,select,button,textarea,a,[role]')].map((element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return {
          tag: element.tagName.toLowerCase(),
          id: element.id || '',
          role: element.getAttribute('role') || '',
          text: (element.innerText || element.value || element.getAttribute('aria-label') || '').trim(),
          ariaLabel: element.getAttribute('aria-label') || '',
          accessibilityName: (() => {
            const explicit = element.getAttribute('aria-label');
            if (explicit) return explicit.trim();
            const labelledBy = element.getAttribute('aria-labelledby');
            if (labelledBy) return labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.innerText || '').join(' ').trim();
            const label = element.closest('label');
            if (!label || label.control !== element) return '';
            const clone = label.cloneNode(true);
            const sourceSelects = [...label.querySelectorAll('select')];
            [...clone.querySelectorAll('select')].forEach((copy, index) => {
              const source = sourceSelects[index];
              if (source === element) copy.remove();
              else {
                const selected = source.options[source.selectedIndex >= 0 ? source.selectedIndex : 0];
                copy.replaceWith(document.createTextNode(selected ? selected.textContent : ''));
              }
            });
            const sourceInputs = [...label.querySelectorAll('input,textarea')];
            [...clone.querySelectorAll('input,textarea')].forEach((copy, index) => {
              if (sourceInputs[index] === element) copy.remove();
            });
            return (clone.innerText || '').trim();
          })(),
          hasAccessibilityName: Boolean(element.getAttribute('aria-label') || element.getAttribute('aria-labelledby') || (element.closest('label') && (element.closest('label').control === element || element.tagName === 'SELECT'))),
          ariaExpanded: element.getAttribute('aria-expanded') || '',
          keyboard: [...element.querySelectorAll('kbd')].map((kbd) => kbd.textContent.trim()).filter(Boolean),
          inputType: element.getAttribute('type') || '',
          placeholder: element.getAttribute('placeholder') || '',
          geometry: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
          visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
          disabled: Boolean(element.disabled),
          focusOutline: style.outlineStyle,
        };
      });
      return {
        title: document.title,
        h1: document.querySelector('h1')?.innerText?.trim() || '',
        bodyText: document.body.innerText,
        controls,
        pageShortcuts: [...document.querySelectorAll('kbd')].map((kbd) => kbd.textContent.trim()).filter(Boolean),
      };
    });

    let ariaSnapshot = '';
    try {
      ariaSnapshot = await page.locator('body').ariaSnapshot();
    } catch (error) {
      ariaSnapshot = `aria snapshot unavailable: ${error}`;
    }

    await fs.writeFile(path.join(screenDir, 'dom.json'), JSON.stringify(dom, null, 2) + '\n');
    await fs.writeFile(path.join(screenDir, 'accessibility.txt'), ariaSnapshot + '\n');
    await fs.writeFile(path.join(screenDir, 'trace.json'), JSON.stringify({ events: trace, requests }, null, 2) + '\n');
    manifest.routes.push({
      route,
      id,
      httpStatus: response?.status() ?? null,
      screenshot: path.relative(outDir, screenshotPath).replaceAll('\\', '/'),
      screenshotSha256: await sha256File(screenshotPath),
      controls: dom.controls.length,
      title: dom.title,
      h1: dom.h1,
      state: stateName,
      actions,
    });
  }

  await fs.writeFile(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  await browser.close();
  console.log(`captured ${manifest.routes.length} reference routes into ${outDir}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});

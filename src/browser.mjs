/**
 * Browser-only observer. Does NOT click Buy/Sell, bypass authentication,
 * scrape TradingView drawing pixels, or access private API endpoints.
 */
import 'dotenv/config';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const profileRoot = path.join(root, 'browser_profiles');
const screenshots = path.join(root, 'screenshots');
fs.mkdirSync(profileRoot, { recursive: true });
fs.mkdirSync(screenshots, { recursive: true });
const contexts = [];
async function openTab(profile, url) {
  const context = await chromium.launchPersistentContext(path.join(profileRoot, profile), {
    headless: false, viewport: { width: 1420, height: 900 }
  });
  contexts.push(context);
  const page = context.pages()[0] || await context.newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  return page;
}
try {
  const tradingview = await openTab('tradingview', 'https://www.tradingview.com/chart/?symbol=NSE%3ANIFTY');
  const kite = await openTab('kite', 'https://kite.zerodha.com/');
  console.log('TradingView and Kite opened. Log in manually, including any 2FA.');
  console.log('Load your own saved TradingView chart layout and AlgoAlpha settings.');
  console.log('Screenshots are local evidence only; signals come from TradingView alerts.');
  async function capture() {
    for (const [name, page] of [['tradingview', tradingview], ['kite', kite]]) {
      try {
        if (!page.isClosed()) {
          await page.screenshot({ path: path.join(screenshots, `${name}-latest.png`), fullPage: false });
        }
      } catch (err) { console.error(`${name} capture skipped: ${err.message}`); }
    }
  }
  await capture();
  const seconds = Math.max(10, Number(process.env.SCREENSHOT_SECONDS ?? 60));
  const timer = setInterval(capture, seconds * 1000);
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
    clearInterval(timer);
    for (const context of contexts) await context.close().catch(() => {});
    process.exit(0);
  });
} catch (e) {
  console.error(`Browser setup failed: ${e.message}`);
  for (const context of contexts) await context.close().catch(() => {});
  process.exitCode = 1;
}

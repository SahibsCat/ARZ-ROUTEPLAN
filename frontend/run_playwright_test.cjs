const { spawn } = require('child_process');
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const ARTIFACT_DIR = 'C:\\Users\\user\\AppData\\Local\\Google\\AndroidStudio2026.1.3\\projects\\rootplan.9617f776\\.artifacts\\bfc74c56-a857-4a18-a9a2-3478fc459f56';

async function waitForServer(url, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const res = await fetch(url);
      if (res.ok || res.status === 200 || res.status === 304) return true;
    } catch {
      // ignore
    }
    await new Promise(r => setTimeout(r, 500));
  }
  return false;
}

(async () => {
  // Start vite preview process
  console.log('Starting vite preview on port 3000...');
  const viteProc = spawn('npx.cmd', ['vite', 'preview', '--port', '3000'], {
    cwd: __dirname,
    stdio: 'inherit',
    shell: true
  });

  const url = 'http://localhost:3000';
  const ready = await waitForServer(url);
  if (!ready) {
    console.error('Server did not respond on time.');
    viteProc.kill();
    process.exit(1);
  }

  console.log('Server is ready! Launching Playwright...');
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();

    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);

    const prefix = process.argv[2] || 'before';
    const screenshotPath = path.join(ARTIFACT_DIR, `${prefix}_ui.png`);
    await page.screenshot({ path: screenshotPath, fullPage: true });
    console.log(`Captured screenshot: ${screenshotPath}`);

    // Try dark mode toggle if present
    const themeBtn = await page.$('.theme-toggle-btn, .theme-toggle, button[title*="theme"], button[aria-label*="theme"]');
    if (themeBtn) {
      await themeBtn.click();
      await page.waitForTimeout(500);
      const darkPath = path.join(ARTIFACT_DIR, `${prefix}_ui_dark.png`);
      await page.screenshot({ path: darkPath, fullPage: true });
      console.log(`Captured dark screenshot: ${darkPath}`);
    }

    // Inspect page dimensions & main elements
    const pageMetrics = await page.evaluate(() => {
      const el = (sel) => document.querySelector(sel);
      return {
        title: document.title,
        sidebarPresent: !!el('.app-sidebar, nav, sidebar'),
        headerPresent: !!el('.app-header, header'),
        mainCardsCount: document.querySelectorAll('.card, .stat-card, .route-card, .order-card').length,
        buttonsCount: document.querySelectorAll('button').length,
        cssVariablesCount: Object.keys(getComputedStyle(document.documentElement)).length
      };
    });
    console.log('Page Metrics:', JSON.stringify(pageMetrics, null, 2));

  } catch (err) {
    console.error('Playwright execution error:', err);
  } finally {
    if (browser) await browser.close();
    viteProc.kill();
    process.exit(0);
  }
})();

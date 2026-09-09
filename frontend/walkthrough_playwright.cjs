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
  const viteProc = spawn('npx.cmd', ['vite', 'preview', '--port', '3000'], {
    cwd: __dirname,
    stdio: 'ignore',
    shell: true
  });

  const url = 'http://localhost:3000';
  const ready = await waitForServer(url);
  if (!ready) {
    console.error('Server did not start');
    viteProc.kill();
    process.exit(1);
  }

  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();

    await page.goto(url, { waitUntil: 'networkidle' });
    await page.waitForTimeout(1000);

    // 1. Dashboard Light View
    await page.screenshot({ path: path.join(ARTIFACT_DIR, '01_dashboard_light.png'), fullPage: true });

    // 2. Open Command Palette (Ctrl+K or click palette button)
    const paletteBtn = await page.$('.topbar__palette-btn, button[title*="Search"]');
    if (paletteBtn) {
      await paletteBtn.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, '02_command_palette.png') });
      const closeBtn = await page.$('.command-palette__close');
      if (closeBtn) await closeBtn.click();
    }

    // 3. Navigate to Drivers board
    const driversNav = await page.$('button:has-text("Drivers")');
    if (driversNav) {
      await driversNav.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, '03_drivers_board.png') });
    }

    // 4. Toggle Dark Theme
    const themeBtn = await page.$('.theme-toggle-btn, .theme-toggle, button[title*="theme"], button[aria-label*="theme"]');
    if (themeBtn) {
      await themeBtn.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, '04_dark_theme_drivers.png'), fullPage: true });
    }

    console.log('Successfully completed Playwright walkthrough screenshots!');
  } catch (err) {
    console.error('Error during walkthrough:', err);
  } finally {
    if (browser) await browser.close();
    viteProc.kill();
    process.exit(0);
  }
})();

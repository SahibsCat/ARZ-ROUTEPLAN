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

    await page.goto(url, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1000);

    console.log('--- PLAYWRIGHT COMPONENT & PAGE ANALYSIS ---');

    // 1. Dashboard View Analysis
    await page.screenshot({ path: path.join(ARTIFACT_DIR, 'analysis_01_dashboard.png'), fullPage: true });
    console.log('Page 1: Dashboard captured');

    // 2. Command Palette (⌘K)
    const paletteBtn = await page.$('.topbar__palette-btn');
    if (paletteBtn) {
      await paletteBtn.click();
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'analysis_02_command_palette.png') });
      const closeBtn = await page.$('.command-palette__close');
      if (closeBtn) await closeBtn.click();
    }

    // 3. Unassigned Orders Section
    const unassignedNav = await page.$('button:has-text("Unassigned Orders")');
    if (unassignedNav) {
      await unassignedNav.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'analysis_03_unassigned_orders.png') });
      console.log('Page 3: Unassigned Orders captured');
    }

    // 4. Failed Addresses Section
    const failedNav = await page.$('button:has-text("Failed Addresses")');
    if (failedNav) {
      await failedNav.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'analysis_04_failed_addresses.png') });
      console.log('Page 4: Failed Addresses captured');
    }

    // 5. Drivers Board
    const driversNav = await page.$('button:has-text("Drivers")');
    if (driversNav) {
      await driversNav.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'analysis_05_drivers_board.png') });
      console.log('Page 5: Drivers Board captured');
    }

    // 6. Toggle Dark Mode & Recapture
    const themeBtn = await page.$('.theme-toggle-btn, .theme-toggle, button[title*="theme"], button[aria-label*="theme"]');
    if (themeBtn) {
      await themeBtn.click();
      await page.waitForTimeout(500);
      await page.screenshot({ path: path.join(ARTIFACT_DIR, 'analysis_06_dark_mode_drivers.png'), fullPage: true });
      console.log('Page 6: Dark Mode captured');
    }

    // DOM metrics analysis across components
    const domAnalysis = await page.evaluate(() => {
      const getStyles = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const comp = getComputedStyle(el);
        return {
          bg: comp.backgroundColor,
          color: comp.color,
          font: comp.fontFamily,
          borderRadius: comp.borderRadius,
          border: comp.border,
          boxShadow: comp.boxShadow
        };
      };

      return {
        body: getStyles('body'),
        topbar: getStyles('.topbar'),
        sidebar: getStyles('.sidebar'),
        navActiveItem: getStyles('.nav-item--active'),
        kpiTile: getStyles('.kpi-tile'),
        primaryBtn: getStyles('.btn--primary'),
        driversTableTh: getStyles('.drivers-table th'),
        editorBox: getStyles('.address-component-editor')
      };
    });

    console.log('DOM Style Analysis:', JSON.stringify(domAnalysis, null, 2));

  } catch (err) {
    console.error('Analysis error:', err);
  } finally {
    if (browser) await browser.close();
    viteProc.kill();
    process.exit(0);
  }
})();

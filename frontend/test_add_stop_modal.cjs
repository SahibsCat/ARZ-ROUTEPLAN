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

    // Click "Add Delivery Point" or "Routes"
    const routesBtn = await page.$('button:has-text("Routes")');
    if (routesBtn) await routesBtn.click();
    await page.waitForTimeout(500);

    // Look for "Add Delivery Point" button
    const addPointBtn = await page.$('button:has-text("Add Delivery Point"), button:has-text("Add Stop"), button:has-text("Add Address")');
    if (addPointBtn) {
      await addPointBtn.click();
      await page.waitForTimeout(600);
      const modalScreenshot = path.join(ARTIFACT_DIR, '07_add_delivery_point_modal_styled.png');
      await page.screenshot({ path: modalScreenshot });
      console.log('Saved modal screenshot to', modalScreenshot);
    } else {
      console.log('Add delivery point button not immediately found on initial state');
    }

  } catch (err) {
    console.error('Test error:', err);
  } finally {
    if (browser) await browser.close();
    viteProc.kill();
    process.exit(0);
  }
})();

const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');

const ARTIFACT_DIR = 'C:\\Users\\user\\AppData\\Local\\Google\\AndroidStudio2026.1.3\\projects\\rootplan.9617f776\\.artifacts\\bfc74c56-a857-4a18-a9a2-3478fc459f56';

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

    // Try port 3000 first, then 5173
    let targetUrl = 'http://localhost:3000';
    try {
      await page.goto(targetUrl, { waitUntil: 'networkidle', timeout: 5000 });
    } catch {
      targetUrl = 'http://localhost:5173';
      await page.goto(targetUrl, { waitUntil: 'networkidle', timeout: 5000 });
    }

    console.log(`Connected to ${targetUrl}`);
    const screenshotPath = path.join(ARTIFACT_DIR, 'before_ui.png');
    await page.screenshot({ path: screenshotPath, fullPage: true });
    console.log(`Saved screenshot to ${screenshotPath}`);

    // Take screenshot in dark mode as well if possible
    const toggle = await page.$('.theme-toggle, [aria-label*="theme"], button:has-text("Theme")');
    if (toggle) {
      await toggle.click();
      await page.waitForTimeout(500);
      const darkPath = path.join(ARTIFACT_DIR, 'before_ui_dark.png');
      await page.screenshot({ path: darkPath, fullPage: true });
      console.log(`Saved dark screenshot to ${darkPath}`);
    }

    await browser.close();
  } catch (err) {
    console.error('Failed to capture UI:', err.message);
    if (browser) await browser.close();
  }
})();

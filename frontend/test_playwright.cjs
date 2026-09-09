const { chromium } = require('playwright-core');

(async () => {
  let browser;
  try {
    browser = await chromium.launch({
      channel: 'msedge',
      headless: true
    });
    console.log('Successfully launched Edge via playwright-core!');
  } catch (err) {
    console.error('Error launching Edge:', err.message);
    try {
      browser = await chromium.launch({
        channel: 'chrome',
        headless: true
      });
      console.log('Successfully launched Chrome via playwright-core!');
    } catch (err2) {
      console.error('Error launching Chrome:', err2.message);
    }
  }

  if (browser) {
    const page = await browser.newPage();
    await page.goto('https://example.com');
    console.log('Page title:', await page.title());
    await browser.close();
  }
})();

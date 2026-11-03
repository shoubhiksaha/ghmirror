// usage: GITLAB_ROOT_PASSWORD=... node shoot.js <baseUrl> <pages.json>   pages: [{name, path, full?, selector?, wait?}]
const { chromium } = require('playwright-core');
const fs = require('fs');
(async () => {
  const [base, pagesFile] = process.argv.slice(2);
  const pages = JSON.parse(fs.readFileSync(pagesFile, 'utf8'));
  const browser = await chromium.launch({ executablePath: process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const page = await ctx.newPage();
  page.setDefaultTimeout(240000);
  await page.goto(base + '/users/sign_in', { waitUntil: 'domcontentloaded' });
  await page.fill('#user_login', 'root');
  await page.fill('#user_password', process.env.GITLAB_ROOT_PASSWORD);
  await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.click('[data-testid="sign-in-button"], button[type=submit]')]);
  for (const p of pages) {
    try {
      await page.goto(base + p.path, { waitUntil: 'networkidle', timeout: 240000 });
    } catch (e) { console.log('slow load', p.name, e.message.split('\n')[0]); }
    // hide one-off nags that are not part of the product view
    await page.addStyleTag({ content: '.gl-alert[data-testid="ssh-key-alert"], .js-ssh-key-alert, .gl-toast, .duo-chat-callout, [data-testid="whats-new-trigger"] {display:none !important}' }).catch(() => {});
    await page.waitForTimeout(p.wait || 2500);
    const out = `shots/${p.name}.png`;
    if (p.selector) await page.locator(p.selector).first().screenshot({ path: out });
    else await page.screenshot({ path: out, fullPage: !!p.full });
    console.log('shot', out);
  }
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });

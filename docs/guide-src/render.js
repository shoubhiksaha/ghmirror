// usage: node render.js <in.html> <out.pdf> <footer:0|1>   also prints heading texts as JSON for the TOC pass
const { chromium } = require('playwright-core');
const path = require('path');
(async () => {
  const [inp, out, footer] = process.argv.slice(2);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  const page = await browser.newPage();
  await page.goto('file://' + path.resolve(inp), { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  const heads = await page.evaluate(() => [...document.querySelectorAll('.pg[data-for]')].map(s => {
    const h = document.getElementById(s.dataset.for); return [s.dataset.for, h ? h.textContent : null]; }));
  await page.pdf({
    path: out, format: 'A4', printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: footer === '1',
    headerTemplate: '<span></span>',
    footerTemplate: `<div style="width:100%;font-family:-apple-system,Helvetica,Arial;font-size:7.5pt;color:#8a94a6;padding:0 17mm;display:flex;justify-content:space-between">
      <span>ghmirror · Architecture, Setup and Operations Guide</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
  });
  console.log(JSON.stringify(heads));
  await browser.close();
})();

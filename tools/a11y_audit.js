// Accessibility audit: axe-core (WCAG 2.0/2.1 A and AA rules) on every page, as an administrator and as a viewer.
// Usage: npm install --prefix tools axe-core@4.10 puppeteer-core@23 && node tools/a11y_audit.js http://127.0.0.1:5000
// Needs a running RouteWise with the demo accounts and Chrome (CHROME_PATH). Exits with 1 when any rule is violated.
const puppeteer = require('puppeteer-core'); const fs = require('fs');
const B = process.argv[2] || 'http://127.0.0.1:5056';
const AXE = fs.readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
(async () => {
  const b = await puppeteer.launch({ executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', args: ['--no-sandbox'], headless: 'new', defaultViewport: { width: 1366, height: 900 } });
  const report = {};
  async function audit(p, label) {
    await p.evaluate(AXE);
    const r = await p.evaluate(async () => await axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] } }));
    report[label] = r.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length, target: v.nodes.slice(0, 3).map(n => n.target.join(' ')) }));
  }
  const p = await b.newPage();
  await p.goto(B + '/login'); await audit(p, 'login');
  for (const [u, pw] of [['admin', 'admin123'], ['viewer', 'viewer123']]) {
    const ctx = await b.createBrowserContext(); const q = await ctx.newPage();
    await q.goto(B + '/login'); await q.type('[name=username]', u); await q.type('[name=password]', pw);
    await Promise.all([q.waitForNavigation(), q.click('button.btn-brand')]);
    for (const path of ['/', '/bookings', '/passengers', '/fleet', '/routes', '/feedback', '/reports', '/board', '/account', '/tickets/1', '/users', '/no-such-page']) {
      if (path === '/users' && u !== 'admin') continue;
      const resp = await q.goto(B + path);
      if (resp.status() >= 500) { report[`${u} ${path}`] = [{ id: `http-${resp.status()}`, impact: 'critical', help: 'The page failed to render', nodes: 1, target: [path] }]; continue; }
      if (path === '/bookings' && u === 'admin') { await q.select('form[data-seatpicker] [name=s_id]', '1'); await new Promise(r => setTimeout(r, 800)); }
      await audit(q, `${u} ${path}`);
    }
    const href = await (await ctx.newPage().then(async n => { await n.goto(B + '/tickets/1'); return n; })).$eval('a[href*="/verify/"]', a => a.getAttribute('href'));
    await q.goto(href); await audit(q, `${u} /verify`);
    await ctx.close();
  }
  await b.close();
  const total = Object.values(report).reduce((n, v) => n + v.length, 0);
  
  const byRule = {};
  for (const [page, vs] of Object.entries(report)) for (const v of vs) { (byRule[v.id] = byRule[v.id] || { impact: v.impact, help: v.help, pages: [], nodes: 0, target: v.target }); byRule[v.id].pages.push(page); byRule[v.id].nodes += v.nodes; }
  console.log(`pages audited: ${Object.keys(report).length}, violations (rule x page): ${total}`);
  for (const [id, v] of Object.entries(byRule)) console.log(`- ${id} [${v.impact}] ${v.help}: ${v.nodes} elements on ${v.pages.length} pages, e.g. ${v.target[0]}`);
  process.exit(total ? 1 : 0);
})();

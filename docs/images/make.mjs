// Regenerates the README images in docs/ from the specs in examples/ and the layout pages
// beside this script:
//   node docs/images/make.mjs
// Maintainer-only. Needs Node 22+ (for the global WebSocket), Google Chrome, ffmpeg 5.1+ and
// the `claude` CLI, without which the page has no translate button. Every image is built in
// a temp dir and copied into docs/ only after all of them succeeded.
// Set CHROME to the browser binary when it is not at the platform's default path.
import { spawn, spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const DOCS = path.join(REPO, 'docs');
const SERVER = path.join(REPO, 'scripts', 'decision_server.py');
const CHROME = process.env.CHROME || (process.platform === 'darwin'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : 'google-chrome');
const TMP = mkdtempSync(path.join(tmpdir(), 'rich-decision-images-'));
const CAP = path.join(TMP, 'cap');
const OUT = path.join(TMP, 'out');

const written = file => console.log(`wrote ${path.relative(REPO, file)} (${Math.round(statSync(file).size / 1024)} KB)`);

// Every child is killed on ANY exit -- an error, Ctrl-C, or a SIGTERM from whatever ran
// this -- because a `finally` alone misses the signal paths and leaves Chrome running.
const children = new Set();
const track = child => { children.add(child); child.once('exit', () => children.delete(child)); return child; };
let ok = false;
process.on('exit', () => {
  for (const c of children) try { c.kill('SIGTERM'); } catch {}
  if (!ok) console.error(`failed; temp files kept in ${TMP}`);
});
for (const [sig, n] of [['SIGINT', 2], ['SIGTERM', 15], ['SIGHUP', 1]]) process.on(sig, () => process.exit(128 + n));

// A page that never loads would otherwise hang the run with no output at all.
function within(promise, ms, what) {
  let timer;
  const expire = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`${what}: no answer after ${ms / 1000} s`)), ms); });
  return Promise.race([promise, expire]).finally(() => clearTimeout(timer));
}

// --- headless Chrome over the DevTools protocol -------------------------------------------

async function launchChrome() {
  const profile = path.join(TMP, 'profile');
  const proc = track(spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--no-default-browser-check', '--hide-scrollbars', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' }));
  proc.on('error', e => { console.error(`cannot start Chrome at ${CHROME}: ${e.message} (set CHROME)`); process.exit(1); });
  const portFile = path.join(profile, 'DevToolsActivePort');
  for (let i = 0; i < 150 && !existsSync(portFile); i++) await sleep(100);
  if (!existsSync(portFile)) throw new Error(`Chrome never opened a debugging port (${CHROME})`);
  const [port, wsPath] = readFileSync(portFile, 'utf8').trim().split('\n');
  const ws = new WebSocket(`ws://127.0.0.1:${port}${wsPath}`);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('cannot connect to Chrome DevTools')); });

  let seq = 0; const pending = new Map(); const listeners = new Set();
  // Chrome crashing must fail every call, in flight or later: a closed socket drops sends
  // silently, so a cleanup call made after the crash would otherwise wait forever.
  let closed = null;
  ws.onclose = () => {
    closed = new Error('Chrome DevTools connection closed');
    for (const { rej } of pending.values()) rej(closed);
    pending.clear();
  };
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const { res, rej } = pending.get(m.id); pending.delete(m.id);
      m.error ? rej(new Error(m.error.message)) : res(m.result);
    } else for (const f of [...listeners]) f(m);
  };
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
    if (closed) return rej(closed);
    const id = ++seq; pending.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params, sessionId }));
  });
  const once = (method, sessionId) => new Promise(res => {
    const f = m => { if (m.method === method && m.sessionId === sessionId) { listeners.delete(f); res(m.params); } };
    listeners.add(f);
  });

  async function page({ width, height, dsf, mobile = false }) {
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    const s = (m, p) => send(m, p, sessionId);
    await s('Page.enable'); await s('Runtime.enable');
    await s('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: dsf, mobile });
    await s('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
    const pg = {
      async goto(url, settle = 1200) {
        const loaded = once('Page.loadEventFired', sessionId);
        const where = url.replace(/\?k=.*/, '');
        // A failed load still fires the load event, on Chrome's error page.
        const { errorText } = await s('Page.navigate', { url });
        if (errorText) throw new Error(`loading ${where}: ${errorText}`);
        await within(loaded, 30000, `loading ${where}`);
        await within(pg.eval('document.fonts.ready.then(() => 1)'), 30000, 'waiting for fonts');
        await sleep(settle);
      },
      async eval(expr) {
        const r = await s('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
        return r.result.value;
      },
      async shot(file) {
        const { data } = await s('Page.captureScreenshot', { format: 'png' });
        writeFileSync(file, Buffer.from(data, 'base64'));
        return file;
      },
      close: () => send('Target.closeTarget', { targetId }),
    };
    return pg;
  }
  // Resolves once Chrome has exited, so its profile dir can be deleted.
  const close = () => new Promise(res => {
    try { ws.close(); } catch {}
    if (proc.exitCode !== null) return res();
    proc.once('exit', res); proc.kill('SIGTERM');
  });
  return { page, close };
}

// --- one decision server per spec, with the languages pinned -------------------------------

// en + zh-Hans on every machine, so the translate button and its labels never depend on
// the maintainer's OS language list.
const XDG = path.join(TMP, 'xdg');
mkdirSync(path.join(XDG, 'rich-decision'), { recursive: true });
writeFileSync(path.join(XDG, 'rich-decision', 'config.json'), '{"primary": "en", "secondary": "zh-Hans"}\n');

async function startServer(name) {
  const p = track(spawn('python3', [SERVER, '--spec', path.join(REPO, 'examples', `${name}.json`),
    '--out', path.join(TMP, `result-${name}.json`), '--no-open', '--no-lan', '--no-sound'],
    { stdio: ['ignore', 'ignore', 'pipe'], env: { ...process.env, XDG_CONFIG_HOME: XDG, PYTHONUNBUFFERED: '1' } }));
  let err = '';
  p.on('error', e => { err += `cannot start python3: ${e.message}\n`; });
  p.stderr.on('data', d => { err += d; });
  for (let i = 0; i < 100 && p.exitCode === null && !err.startsWith('cannot start'); i++) {
    const m = err.match(/http:\/\/127\.0\.0\.1:\d+\/\?k=[0-9a-f]+/);
    if (m) return { url: m[0], stop: () => p.kill('SIGTERM') };
    await sleep(100);
  }
  p.kill('SIGTERM');
  throw new Error(`decision server did not start for ${name}:\n${err}`);
}

// --- captures ------------------------------------------------------------------------------

const rectOf = (pg, sel) => pg.eval(`(() => { const r = document.querySelector(${JSON.stringify(sel)}).getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height }; })()`);

// Gallery tiles: 1280 x 1040 CSS px at 1.25x is the committed 1600 x 1300 file, no resize step.
async function tile(browser, name, out, prepare) {
  const srv = await startServer(name);
  const pg = await browser.page({ width: 1280, height: 1040, dsf: 1.25 });
  try {
    await pg.goto(srv.url);
    // Clicked cards animate (.card has a 0.12 s transition); a shot taken mid-way differs run to run.
    if (prepare) { await pg.eval(prepare); await sleep(500); }
    await pg.shot(out);
  } finally { await pg.close(); srv.stop(); }
}

// The pick-a-store page through select -> note -> confirm, for the GIF and the before/after.
async function storeStates(browser) {
  const srv = await startServer('pick-a-store');
  const pg = await browser.page({ width: 1280, height: 800, dsf: 2 });
  try {
    await pg.goto(srv.url);
    await pg.shot(path.join(CAP, 'store-0.png'));
    const rects = { hive: await rectOf(pg, '.card'), notes: await rectOf(pg, '#notes'), submit: await rectOf(pg, '#submit') };
    await pg.eval(`document.querySelector('.card').click(), 1`);
    await sleep(250); await pg.shot(path.join(CAP, 'store-1.png'));
    const note = 'no native deps wins';
    for (const [i, n] of [4, 9, 14, note.length].entries()) {
      await pg.eval(`(() => { const t = document.getElementById('notes'); t.focus(); t.value = ${JSON.stringify(note)}.slice(0, ${n}); return 1; })()`);
      await sleep(120); await pg.shot(path.join(CAP, `store-2-${i}.png`));
    }
    await pg.eval(`document.getElementById('submit').click(), 1`);
    await sleep(900); await pg.shot(path.join(CAP, 'store-3.png'));
    return rects;
  } finally { await pg.close(); srv.stop(); }
}

// The same page on a phone, before and after the translate button. Runs first: the server
// hides that button on EVERY page when the `claude` CLI is missing, so going on without it
// would rewrite the whole set without the button.
async function phoneStates(browser) {
  const srv = await startServer('pick-a-store');
  const pg = await browser.page({ width: 390, height: 844, dsf: 3, mobile: true });
  try {
    await pg.goto(srv.url);
    if (await pg.eval(`document.getElementById('lang').hidden`)) {
      throw new Error('the translate button is hidden, so the `claude` CLI was not found; nothing in docs/ was changed');
    }
    await pg.shot(path.join(CAP, 'phone-en.png'));
    await pg.eval(`document.getElementById('lang').click(), 1`);
    const t0 = Date.now();
    // The button stays disabled while a translation runs. Once it is enabled again, a visible
    // #lang-err means a failed or partial translation, which must not become a README image.
    for (;;) {
      const st = await pg.eval(`(() => { const b = document.getElementById('lang'), e = document.getElementById('lang-err');
        return { busy: b.disabled, label: b.textContent, err: e.hidden ? '' : e.textContent }; })()`);
      if (!st.busy && st.err) throw new Error(`translation failed or incomplete: ${st.err}`);
      if (!st.busy && st.label === 'English') break;
      if (Date.now() - t0 > 120000) throw new Error('translation did not finish within 120 s');
      await sleep(500);
    }
    console.log(`translated in ${((Date.now() - t0) / 1000).toFixed(1)} s`);
    await pg.shot(path.join(CAP, 'phone-zh.png'));
  } finally { await pg.close(); srv.stop(); }
}

// --- composites ----------------------------------------------------------------------------

// The layout pages load their captures as cap/*.png, so they run from a copy beside cap/.
async function composite(browser, page, size, out) {
  copyFileSync(path.join(HERE, page), path.join(TMP, page));
  const pg = await browser.page(size);
  try { await pg.goto(pathToFileURL(path.join(TMP, page)).href, 400); await pg.shot(out); }
  finally { await pg.close(); }
}

async function demoGif(browser, R) {
  const frames = path.join(TMP, 'frames'); mkdirSync(frames);
  copyFileSync(path.join(HERE, 'stage.html'), path.join(TMP, 'stage.html'));
  const pg = await browser.page({ width: 1280, height: 800, dsf: 2 });
  await pg.goto(pathToFileURL(path.join(TMP, 'stage.html')).href, 300);

  const USER = 'Which local store should the sync layer use? Show me the options.';
  const user = (t, caret) => `<span class="p">&gt;</span> <span class="u">${t}</span>${caret ? '<span class="caret"></span>' : ''}`;
  const asked = [user(USER), '',
    '<span class="dot">&#9679;</span> Three real candidates, and the trade-offs are easier to see',
    '  side by side. Opened a decision page; waiting for your pick...'];
  const answered = [...asked,
    '  <span class="dim">&#9492; {"choice": ["hive"], "notes": "no native deps wins"}</span>', '',
    '<span class="dot">&#9679;</span> Going with Hive. Adding it to pubspec.yaml and writing a box',
    '  adapter for Note.'];
  const term = asked.join('\n');

  const seq = [];                                       // [stage state, seconds on screen]
  const add = (s, d) => seq.push([s, d]);
  add({ term: user('', true) }, 0.7);
  for (let n = 8; n < USER.length; n += 9) add({ term: user(USER.slice(0, n), true) }, 0.07);
  add({ term: user(USER, true) }, 0.6);
  add({ term: asked.slice(0, 3).join('\n') }, 0.35);
  add({ term }, 0.9);
  const move = (img, from, to) => {                     // eased cursor glide, 6 frames
    for (let i = 1; i <= 6; i++) {
      const t = i / 6, e = t < .5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      add({ term, img, cursor: [from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e] }, 0.045);
    }
    return to;
  };
  let at = [980, 560];
  add({ term, img: 'cap/store-0.png', cursor: at }, 1.6);
  at = move('cap/store-0.png', at, [R.hive.x + R.hive.w * 0.35, R.hive.y + 60]);
  add({ term, img: 'cap/store-0.png', cursor: at }, 0.25);
  add({ term, img: 'cap/store-1.png', cursor: at }, 1.0);
  at = move('cap/store-1.png', at, [R.notes.x + 160, R.notes.y + 22]);
  for (let i = 0; i < 4; i++) add({ term, img: `cap/store-2-${i}.png`, cursor: at }, i === 3 ? 0.7 : 0.14);
  at = move('cap/store-2-3.png', at, [R.submit.x + R.submit.w * 0.45, R.submit.y + R.submit.h * 0.55]);
  add({ term, img: 'cap/store-2-3.png', cursor: at }, 0.3);
  add({ term, img: 'cap/store-3.png', cursor: at }, 1.0);
  add({ term: answered.slice(0, 5).join('\n') }, 0.6);
  add({ term: answered.join('\n') }, 3.6);

  const list = [];
  for (const [i, [state, secs]] of seq.entries()) {
    await pg.eval(`setState(${JSON.stringify(state)})`);
    const f = await pg.shot(path.join(frames, `f${String(i).padStart(3, '0')}.png`));
    list.push(`file '${f}'`, `duration ${secs}`);
  }
  await pg.close();
  list.push(list.at(-2));                               // concat honours the last duration only if the file repeats
  writeFileSync(path.join(frames, 'list.txt'), list.join('\n') + '\n');

  const out = path.join(OUT, 'demo.gif');
  const r = spawnSync('ffmpeg', ['-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', path.join(frames, 'list.txt'),
    '-vf', 'scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle',
    '-fps_mode', 'vfr', '-loop', '0', out], { stdio: 'inherit' });
  if (r.error || r.status !== 0) throw new Error(`ffmpeg failed (${r.error?.message || `exit ${r.status}`})`);
  console.log(`${seq.length} frames, ${seq.reduce((a, [, d]) => a + d, 0).toFixed(1)} s`);
}

// --- main ----------------------------------------------------------------------------------

mkdirSync(CAP); mkdirSync(OUT);
let browser;
try {
  browser = await launchChrome();
  await phoneStates(browser);                           // first: it is also the claude CLI check
  const rects = await storeStates(browser);
  await tile(browser, 'several-questions', path.join(OUT, 'gallery-questions.png'),
    `(() => { const q = document.querySelectorAll('.question');
      q[0].querySelectorAll('.card')[0].click();
      q[1].querySelectorAll('.card')[0].click(); q[1].querySelectorAll('.card')[1].click();
      q[1].querySelector('textarea.q-notes').value = 'Background sync can wait for v2'; return 1; })()`);
  await tile(browser, 'explainer', path.join(OUT, 'gallery-explainer.png'));
  await tile(browser, 'ui-mockups', path.join(OUT, 'gallery-mockups.png'));
  await composite(browser, 'phone.html', { width: 1280, height: 1040, dsf: 1.25 }, path.join(OUT, 'gallery-phone.png'));
  await composite(browser, 'before-after.html', { width: 1440, height: 548, dsf: 2 }, path.join(OUT, 'before-after.png'));
  await demoGif(browser, rects);
  // All six exist: only now does docs/ change, so a failure never leaves a mixed set.
  for (const f of readdirSync(OUT)) { copyFileSync(path.join(OUT, f), path.join(DOCS, f)); written(path.join(DOCS, f)); }
  ok = true;
} finally {
  if (browser) await browser.close();
  if (ok) rmSync(TMP, { recursive: true, force: true });
}

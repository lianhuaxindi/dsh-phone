import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir, stat } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

async function listFiles(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) files.push(...(await listFiles(full)));
    else files.push(full);
  }
  return files;
}

const JS_FILES = (await listFiles(join(ROOT, 'js'))).filter((file) => file.endsWith('.js')).sort();

test('index.html loads relative modules and styles only (no build step)', async () => {
  const html = await readFile(join(ROOT, 'index.html'), 'utf8');
  assert.match(html, /<meta charset="utf-8"\s*\/?>/i);
  assert.match(html, /name="viewport"[^>]*viewport-fit=cover/);
  assert.match(html, /<script type="module" src="\.\/js\/app\.js"><\/script>/);
  assert.match(html, /<div id="app"/);

  const hrefs = [...html.matchAll(/(?:src|href)="([^"]+)"/g)].map((match) => match[1]);
  assert.ok(hrefs.length >= 4);
  for (const href of hrefs) {
    assert.ok(href.startsWith('./'), `asset must use a relative path: ${href}`);
  }
  for (const href of hrefs) {
    const file = fileURLToPath(new URL(href, `file://${join(ROOT, 'index.html')}`));
    await assert.doesNotReject(() => stat(file), `referenced asset exists: ${href}`);
  }
  assert.equal(/https?:\/\//.test(html.replace(/<!--[\s\S]*?-->/g, '')), false, 'no remote assets');
  assert.equal(/\b(createElement|React|Vue)\b/.test(html), false, 'no framework bootstrap');
});

test('every module in js/ imports cleanly in Node (no DOM required)', async () => {
  for (const file of JS_FILES) {
    const specifier = new URL(`file://${file}`).href;
    await assert.doesNotReject(() => import(specifier), `module loads: ${relative(ROOT, file)}`);
  }
});

test('relative import graph resolves to real files', async () => {
  for (const file of JS_FILES) {
    const source = await readFile(file, 'utf8');
    const specifiers = [
      ...source.matchAll(/^\s*(?:import|export)\s[^'"]*from\s+['"](\.[^'"]+)['"]/gm),
      ...source.matchAll(/^\s*import\s+['"](\.[^'"]+)['"]/gm),
    ].map((match) => match[1]);
    for (const specifier of specifiers) {
      const target = resolve(dirname(file), specifier);
      await assert.doesNotReject(() => stat(target), `${relative(ROOT, file)} → ${specifier}`);
    }
  }
});

test('app shell exports the screen router and demo entry points', async () => {
  const module = await import(new URL(`file://${join(ROOT, 'js/app.js')}`).href);
  for (const name of ['SCREEN', 'WebApp', 'createApp']) {
    assert.ok(name in module, `app.js exports ${name}`);
  }
  assert.equal(typeof module.createApp, 'function');
  assert.deepEqual(Object.values(module.SCREEN).sort(), ['chat', 'diagnostics', 'pairing', 'sessions', 'settings']);
});

test('views and UI modules export mountable factories', async () => {
  const cases = [
    ['js/views/pairing.js', 'createPairingView'],
    ['js/views/sessions.js', 'createSessionsView'],
    ['js/views/chat.js', 'createChatView'],
    ['js/views/settings.js', 'createSettingsView'],
    ['js/views/diagnostics.js', 'createDiagnosticsView'],
    ['js/ui/approval-sheet.js', 'openApprovalSheet'],
    ['js/ui/question-sheet.js', 'openQuestionSheet'],
    ['js/ui/recording-overlay.js', 'RecordingOverlay'],
    ['js/ui/toast.js', 'ToastHost'],
    ['js/ui/sheet.js', 'openSheet'],
  ];
  for (const [file, exportName] of cases) {
    const module = await import(new URL(`file://${join(ROOT, file)}`).href);
    assert.equal(typeof module[exportName], 'function', `${file} exports ${exportName}`);
  }
});

test('literal UI copy keys used via t() exist in the dictionary', async () => {
  const i18n = await import(new URL(`file://${join(ROOT, 'js/i18n.js')}`).href);
  const files = [...JS_FILES];
  let checked = 0;
  const missing = new Set();
  for (const file of files) {
    const source = await readFile(file, 'utf8');
    for (const match of source.matchAll(/\bt\(\s*'([a-z0-9][a-z0-9.-]*)'/g)) {
      const key = match[1];
      if (key.includes('.${')) continue;
      checked += 1;
      if (!i18n.hasKey(key)) missing.add(`${key} (${relative(ROOT, file)})`);
    }
  }
  assert.ok(checked > 80, `found ${checked} literal t() keys`);
  assert.deepEqual([...missing], []);
});

test('no npm dependencies or build artifacts ship with the web UI', async () => {
  const pkg = JSON.parse(await readFile(join(ROOT, 'package.json'), 'utf8'));
  assert.equal(pkg.type, 'module');
  assert.equal(pkg.private, true);
  assert.equal(pkg.dependencies ?? null, null);
  assert.equal(pkg.devDependencies ?? null, null);
  const entries = await readdir(ROOT);
  for (const forbidden of ['node_modules', 'dist', 'build', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock']) {
    assert.equal(entries.includes(forbidden), false, `no ${forbidden} in the web UI`);
  }
  assert.equal((await listFiles(ROOT)).some((file) => file.endsWith('.map')), false, 'no source maps');
});

test('css exposes design tokens, 48dp targets and reduced motion support', async () => {
  const tokens = await readFile(join(ROOT, 'css/tokens.css'), 'utf8');
  assert.match(tokens, /--touch:\s*48px/);
  assert.match(tokens, /prefers-color-scheme:\s*dark/);
  assert.match(tokens, /prefers-reduced-motion/);
  const components = await readFile(join(ROOT, 'css/components.css'), 'utf8');
  assert.match(components, /min-height:\s*var\(--touch\)/);
  assert.match(components, /\.message--streaming/);
  assert.match(components, /\.approval__command/);
  const app = await readFile(join(ROOT, 'css/app.css'), 'utf8');
  assert.match(app, /\.tabbar/);
});

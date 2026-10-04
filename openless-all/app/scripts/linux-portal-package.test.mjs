import assert from 'node:assert/strict';
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

if (process.platform !== 'linux' || spawnSync('dpkg-deb', ['--version']).status !== 0) {
  console.log('linux-portal-package.test.mjs skipped: Debian packaging tools required');
  process.exit(0);
}

// Exercise the actual packager on Linux; no fcitx5 build or rpm tools needed.
const app = fileURLToPath(new URL('..', import.meta.url));
const work = mkdtempSync(join(tmpdir(), 'openless-portal-package-'));
function run(command, args, env = {}) {
  const result = spawnSync(command, args, { encoding: 'utf8', env: { ...process.env, ...env } });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return result.stdout;
}
try {
  mkdirSync(join(work, 'release'));
  copyFileSync('/usr/bin/true', join(work, 'release/openless-linux-egui'));
  const version = JSON.parse(readFileSync(join(app, 'package.json'))).version.split('+')[0];
  const revision = readFileSync(join(app, 'linux-egui/package-revision'), 'utf8').trim();
  run('bash', [join(app, 'scripts/package-linux-egui.sh')], {
    CARGO_TARGET_DIR: work,
    OPENLESS_LINUX_VERSION: `${version}-${revision}`,
    OPENLESS_LINUX_INPUT_BACKEND: 'portal',
  });
  const output = join(work, 'linux-egui-packages');
  const files = readdirSync(output);
  assert.equal(files.length, 1);
  assert.match(files[0], /-portal-.*\.deb$/);
  const deb = join(output, files[0]);
  const control = run('dpkg-deb', ['--field', deb]);
  assert.match(control, /python3-gi/);
  assert.match(control, /xdg-desktop-portal/);
  assert.doesNotMatch(control, /fcitx/);
  const contents = run('dpkg-deb', ['--contents', deb]);
  assert.match(contents, /usr\/bin\/openless/);
  assert.match(contents, /top.openless.OpenLess.desktop/);
  assert.doesNotMatch(contents, /fcitx/);
  run('dpkg-deb', ['--control', deb, join(work, 'control')]);
  assert.deepEqual(readdirSync(join(work, 'control')), ['control']);
  console.log('linux-portal-package.test.mjs passed');
} finally {
  rmSync(work, { recursive: true, force: true });
}

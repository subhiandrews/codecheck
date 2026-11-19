import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { formatDoctor, runDoctor } from '../installer/lib/doctor.js';
import { install } from '../installer/lib/install.js';
import { uninstall } from '../installer/lib/uninstall.js';
import { fakeExec, makeEnv } from './helpers.js';

function fixture({ installed = true, loggedIn = true } = {}) {
  const { root, env } = makeEnv();
  env.CODEX_HOME = path.join(root, 'different-codex-home');
  const fallback = fakeExec();
  const exec = (cmd, args, opts) => {
    if (cmd !== 'codex') return fallback(cmd, args, opts);
    if (!installed) return { status: 127, stderr: 'not found' };
    if (args[0] === '--version') return { status: 0, stdout: 'codex-cli 0.170.0\n' };
    assert.deepEqual(args, ['login', 'status']);
    return { status: loggedIn ? 0 : 1, stderr: loggedIn ? 'Logged in using ChatGPT\n' : 'Not logged in\n' };
  };
  return {
    root, exec, env, fallback,
    skills: path.join(env.HOME, '.agents', 'skills'),
    receiptPath: path.join(env.CODE_CHECK_HOME, 'codex', 'receipt.json'),
  };
}

test('Codex doctor checks version and login, with no Claude invocation', () => {
  for (const options of [{}, { installed: false }, { loggedIn: false }]) {
    const { exec, fallback } = fixture(options);
    const result = runDoctor({ exec, target: 'codex' });
    assert.equal(result.ok, !Object.keys(options).length);
    assert.ok(!fallback.calls.some(({ cmd }) => cmd === 'claude'));
    const checks = Object.fromEntries(result.checks.map((c) => [c.name, c]));
    assert.equal(checks.codex.ok, options.installed !== false);
    if (options.installed !== false) assert.match(checks.codex.detail, /0\.170\.0/);
    assert.equal(checks['codex login'].ok, result.ok);
  }
});

test('doctor warns only for the Codex target on an unmeasured platform', () => {
  const { exec } = fixture();
  const unmeasured = runDoctor({ exec, target: 'codex', platform: 'linux' });
  assert.ok(unmeasured.ok);
  assert.match(formatDoctor(unmeasured), /\[warn\] codex platform: .*macOS/);
  assert.doesNotMatch(formatDoctor(runDoctor({ exec, target: 'codex', platform: 'darwin' })), /\[warn\] codex platform/);
  assert.doesNotMatch(formatDoctor(runDoctor({ exec, target: 'claude', platform: 'linux' })), /codex platform/);
});

test('Codex install renders the absolute launcher path, records only owned files and preserves Claude data', () => {
  const { env, exec, skills, receiptPath, fallback } = fixture();
  const foreign = path.join(skills, 'babysit-pr', 'SKILL.md');
  fs.mkdirSync(path.dirname(foreign), { recursive: true });
  fs.writeFileSync(foreign, 'foreign');
  fs.mkdirSync(env.CODE_CHECK_HOME);
  const claudeReceipt = path.join(env.CODE_CHECK_HOME, 'receipt.json');
  fs.writeFileSync(claudeReceipt, 'existing Claude receipt');

  const result = install({ exec, env, target: 'codex' });
  const receipt = JSON.parse(fs.readFileSync(receiptPath, 'utf8'));
  assert.deepEqual(receipt, result);
  assert.deepEqual(receipt.files.map((file) => path.basename(path.dirname(file))).sort(),
    ['address-comments', 'review-local', 'review-pr']);
  const bin = path.join(env.CODE_CHECK_HOME, 'codex', 'runtime', 'bin', 'code-check');
  fs.accessSync(bin, fs.constants.X_OK);
  for (const file of receipt.files) {
    assert.equal(path.dirname(path.dirname(file)), skills);
    const text = fs.readFileSync(file, 'utf8');
    assert.ok(text.includes(`"${bin}"`));
    assert.ok(!text.includes('@CODE_CHECK_BIN@'));
  }
  assert.equal(fs.readFileSync(foreign, 'utf8'), 'foreign');
  assert.equal(fs.readFileSync(claudeReceipt, 'utf8'), 'existing Claude receipt');
  assert.equal(fs.existsSync(env.CODEX_HOME), false);
  assert.ok(!fallback.calls.some(({ cmd }) => cmd === 'claude'));
  assert.deepEqual(install({ exec, env, target: 'codex' }).files, receipt.files);
});

test('Codex uninstall removes receipt skills only and refuses outside and symlink paths', () => {
  const { root, env, exec, skills, receiptPath } = fixture();
  const installed = install({ exec, env, target: 'codex' });
  const notes = path.join(path.dirname(installed.files[0]), 'notes.txt');
  fs.writeFileSync(notes, 'user file');
  const outside = path.join(root, 'outside', 'SKILL.md');
  fs.mkdirSync(path.dirname(outside));
  fs.writeFileSync(outside, 'outside');
  const linked = path.join(skills, 'linked');
  fs.symlinkSync(path.dirname(outside), linked, 'dir');
  const foreign = path.join(skills, 'foreign', 'SKILL.md');
  fs.mkdirSync(path.dirname(foreign));
  fs.writeFileSync(foreign, 'foreign');
  fs.writeFileSync(receiptPath, JSON.stringify({ ...installed, files: [...installed.files, outside, path.join(linked, 'SKILL.md')] }));
  const messages = [];
  uninstall({ exec, env, target: 'codex', log: (message) => messages.push(message) });
  for (const file of installed.files) assert.equal(fs.existsSync(file), false);
  assert.equal(fs.readFileSync(notes, 'utf8'), 'user file');
  assert.equal(fs.readFileSync(foreign, 'utf8'), 'foreign');
  assert.equal(fs.readFileSync(outside, 'utf8'), 'outside');
  assert.ok(fs.lstatSync(linked).isSymbolicLink());
  assert.equal(messages.filter((message) => message.includes('refusing')).length, 2);
  assert.equal(fs.existsSync(receiptPath), false);
  uninstall({ exec, env, target: 'codex' });
  assert.equal(fs.readFileSync(foreign, 'utf8'), 'foreign');
});

test('Codex install aborts before installing a skill if a placeholder would remain', () => {
  const { env, exec, skills } = fixture();
  env.CODE_CHECK_HOME = path.join(env.CODE_CHECK_HOME, '@CODE_CHECK_BIN@');
  assert.throws(() => install({ env, exec, target: 'codex' }), /unresolved CLI placeholder/);
  assert.equal(fs.existsSync(skills), false);
  assert.equal(fs.existsSync(env.CODE_CHECK_HOME), false);
});

test('the CLI accepts the Codex uninstall target and rejects invalid targets', () => {
  const { env } = fixture();
  const cli = new URL('../installer/cli.js', import.meta.url).pathname;
  for (const flag of ['--target=codex', '--target=bogus']) {
    const result = spawnSync(process.execPath, [cli, 'uninstall', flag], { encoding: 'utf8', env });
    if (flag.endsWith('codex')) assert.match(result.stdout, /no Codex install receipt found/);
    else assert.match(result.stderr, /--target must be claude or codex/);
    assert.equal(result.status, flag.endsWith('codex') ? 0 : 1);
  }
});

test('a corrupt Codex receipt names the file and says how to recover', () => {
  const { exec, env, receiptPath } = fixture();
  install({ exec, env, target: 'codex' });
  fs.writeFileSync(receiptPath, '{not json');
  assert.throws(() => install({ exec, env, target: 'codex' }), (error) => error.message.includes(receiptPath) && /reinstall/.test(error.message));
  assert.throws(() => uninstall({ exec, env, target: 'codex' }), (error) => error.message.includes(receiptPath));
});

test('uninstall rejects an unknown target instead of running the Claude uninstaller', () => {
  const { exec, env, fallback } = fixture();
  assert.throws(() => uninstall({ exec, env, target: 'Codex' }), /--target must be claude or codex/);
  assert.ok(!fallback.calls.some(({ cmd }) => cmd === 'claude'));
});

test('Codex install refuses a runtime stage without the ownership marker', () => {
  const { exec, env } = fixture();
  fs.mkdirSync(path.join(env.CODE_CHECK_HOME, 'codex', 'runtime'), { recursive: true });
  assert.throws(() => install({ exec, env, target: 'codex' }), /no Codex ownership marker/);
});

test('Codex install refuses a symlinked or non-directory skills root', () => {
  const linked = fixture();
  const elsewhere = path.join(linked.root, 'elsewhere');
  fs.mkdirSync(elsewhere);
  fs.mkdirSync(path.join(linked.env.HOME, '.agents'), { recursive: true });
  fs.symlinkSync(elsewhere, linked.skills);
  assert.throws(() => install({ exec: linked.exec, env: linked.env, target: 'codex' }), /expected a directory without symlinks/);
  assert.deepEqual(fs.readdirSync(elsewhere), []);

  const file = fixture();
  fs.mkdirSync(path.join(file.env.HOME, '.agents'), { recursive: true });
  fs.writeFileSync(file.skills, 'not a directory');
  assert.throws(() => install({ exec: file.exec, env: file.env, target: 'codex' }), /expected a directory without symlinks/);
});

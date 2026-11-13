import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { install } from '../installer/lib/install.js';
import { uninstall } from '../installer/lib/uninstall.js';
import { fakeExec, makeEnv } from './helpers.js';

test('plugin mode: stages the payload, sets up the runtime, registers through the claude CLI', () => {
  const { env, root } = makeEnv();
  const exec = fakeExec();
  const result = install({ exec, env });
  const stage = path.join(root, 'data', 'plugin');
  assert.equal(result.mode, 'plugin');
  assert.ok(fs.existsSync(path.join(stage, 'skills', 'review-local', 'SKILL.md')));
  assert.ok(fs.existsSync(path.join(stage, 'agents', 'aggregator.md')));
  assert.ok(fs.statSync(path.join(stage, 'bin', 'code-check')).mode & 0o111);
  const lines = exec.lines();
  const idx = (s) => lines.findIndex((l) => l.includes(s));
  assert.ok(idx('ensure-runtime.sh') >= 0 && idx('gen-agents') > idx('ensure-runtime.sh'));
  assert.ok(idx(`plugin marketplace add ${stage}`) > idx('gen-agents'));
  assert.ok(idx('plugin install code-check@code-check') > idx('plugin marketplace add'));
  const receipt = JSON.parse(fs.readFileSync(path.join(root, 'data', 'receipt.json'), 'utf8'));
  assert.equal(receipt.mode, 'plugin');
  assert.equal(fs.existsSync(path.join(env.CLAUDE_CONFIG_DIR, 'skills')), false);
});

test('copy mode when claude is missing: rewrites variables, keeps foreign skills, is idempotent', () => {
  const { env, root } = makeEnv();
  const foreign = path.join(env.CLAUDE_CONFIG_DIR, 'skills', 'other-skill');
  fs.mkdirSync(foreign, { recursive: true });
  fs.writeFileSync(path.join(foreign, 'SKILL.md'), 'mine');
  const logs = [];
  const first = install({ exec: fakeExec({ claude: false }), env, log: (m) => logs.push(m) });
  assert.equal(first.mode, 'copy');
  const installed = path.join(env.CLAUDE_CONFIG_DIR, 'skills', 'review-pr', 'SKILL.md');
  const text = fs.readFileSync(installed, 'utf8');
  assert.ok(!text.includes('${CLAUDE_PLUGIN_ROOT}'));
  assert.ok(text.includes(path.join(root, 'data', 'plugin', 'bin', 'code-check')));
  assert.ok(!text.includes('code-check:'));
  assert.ok(fs.existsSync(path.join(env.CLAUDE_CONFIG_DIR, 'agents', 'architect.md')));
  assert.equal(fs.readFileSync(path.join(foreign, 'SKILL.md'), 'utf8'), 'mine');
  assert.ok(logs.some((m) => /claude not found/.test(m)));
  const second = install({ exec: fakeExec({ claude: false }), env });
  assert.deepEqual(second.files.sort(), first.files.sort());
  assert.equal(fs.readFileSync(installed, 'utf8'), text);
});

test('falls back to copy mode when the claude plugin commands fail', () => {
  const { env } = makeEnv();
  const result = install({ exec: fakeExec({ claudeFailsOn: ['plugin marketplace add'] }), env });
  assert.equal(result.mode, 'copy');
});

test('re-install removes the previous registration first', () => {
  const { env } = makeEnv();
  const exec = fakeExec();
  install({ exec, env });
  const lines = exec.lines();
  assert.ok(lines.findIndex((l) => l.includes('plugin uninstall')) < lines.findIndex((l) => l.includes('plugin marketplace add')));
});

test('missing prerequisites abort before touching anything', () => {
  const { env, root } = makeEnv();
  assert.throws(() => install({ exec: fakeExec({ gh: false }), env }), /prerequisites missing.*gh/);
  assert.throws(() => install({ exec: fakeExec({ python: null }), env }), /python/);
  assert.equal(fs.existsSync(path.join(root, 'data')), false);
  assert.equal(fs.existsSync(env.CLAUDE_CONFIG_DIR), false);
});

test('runtime setup failure is reported', () => {
  const { env } = makeEnv();
  const base = fakeExec();
  const exec = (cmd, args = [], opts = {}) =>
    cmd === 'sh' ? { status: 1, stdout: '', stderr: 'pip exploded' } : base(cmd, args, opts);
  assert.throws(() => install({ exec, env }), /runtime setup failed.*pip exploded/);
});

test('relative or option-like data dirs are resolved to absolute paths', () => {
  const { env } = makeEnv();
  const cwd = process.cwd();
  const tmp = path.dirname(env.HOME);
  process.chdir(tmp);
  try {
    const exec = fakeExec();
    const r = install({ exec, env: { ...env, CODE_CHECK_HOME: '-rel-data' } });
    assert.ok(path.isAbsolute(r.stage));
    assert.ok(exec.calls.every((c) => c.args.every((a) => !a.startsWith('-rel'))));
  } finally {
    process.chdir(cwd);
  }
});

test('copy mode never clobbers user-owned skills/agents (incl. symlinks) and records only what it created', () => {
  const { env, root } = makeEnv();
  const agents = path.join(env.CLAUDE_CONFIG_DIR, 'agents');
  const skills = path.join(env.CLAUDE_CONFIG_DIR, 'skills');
  fs.mkdirSync(agents, { recursive: true });
  fs.mkdirSync(path.join(skills, 'review-pr'), { recursive: true });
  fs.writeFileSync(path.join(agents, 'architect.md'), 'mine');
  fs.writeFileSync(path.join(skills, 'review-pr', 'SKILL.md'), 'mine too');
  const target = path.join(root, 'elsewhere.md');
  fs.writeFileSync(target, 'linked');
  fs.symlinkSync(target, path.join(agents, 'skeptic.md'));
  const logs = [];
  const r = install({ exec: fakeExec({ claude: false }), env, log: (m) => logs.push(m) });
  assert.equal(fs.readFileSync(path.join(agents, 'architect.md'), 'utf8'), 'mine');
  assert.equal(fs.readFileSync(path.join(skills, 'review-pr', 'SKILL.md'), 'utf8'), 'mine too');
  assert.equal(fs.readFileSync(target, 'utf8'), 'linked');
  assert.ok(!r.files.includes(path.join(agents, 'architect.md')));
  assert.ok(!r.files.includes(path.join(skills, 'review-pr')));
  assert.ok(!r.files.includes(path.join(agents, 'skeptic.md')));
  assert.ok(r.files.includes(path.join(agents, 'aggregator.md')));
  assert.ok(logs.filter((m) => /not managed by code-check/.test(m)).length >= 3);
  // re-install and uninstall leave the foreign entries alone
  install({ exec: fakeExec({ claude: false }), env });
  uninstall({ exec: fakeExec({ claude: false }), env });
  assert.equal(fs.readFileSync(path.join(agents, 'architect.md'), 'utf8'), 'mine');
  assert.equal(fs.readFileSync(path.join(skills, 'review-pr', 'SKILL.md'), 'utf8'), 'mine too');
  assert.ok(fs.lstatSync(path.join(agents, 'skeptic.md')).isSymbolicLink());
  assert.equal(fs.existsSync(path.join(agents, 'aggregator.md')), false);
});

test('mode switch copy -> plugin removes the previously copied files', () => {
  const { env } = makeEnv();
  install({ exec: fakeExec({ claude: false }), env });
  const copied = path.join(env.CLAUDE_CONFIG_DIR, 'agents', 'aggregator.md');
  assert.ok(fs.existsSync(copied));
  const r = install({ exec: fakeExec(), env });
  assert.equal(r.mode, 'plugin');
  assert.equal(fs.existsSync(copied), false);
  assert.equal(fs.existsSync(path.join(env.CLAUDE_CONFIG_DIR, 'skills', 'review-pr')), false);
});

test('mode switch plugin -> copy unregisters the old plugin', () => {
  const { env } = makeEnv();
  install({ exec: fakeExec(), env });
  const exec = fakeExec({ claudeFailsOn: ['plugin marketplace add'] });
  const r = install({ exec, env });
  assert.equal(r.mode, 'copy');
  assert.ok(exec.lines().some((l) => l.includes('plugin uninstall code-check@code-check')));
  assert.ok(exec.lines().some((l) => l.includes('plugin marketplace remove code-check')));
});

test('re-install with a corrupt receipt still works', () => {
  const { env, root } = makeEnv();
  install({ exec: fakeExec({ claude: false }), env });
  fs.writeFileSync(path.join(root, 'data', 'receipt.json'), '{not json');
  assert.equal(install({ exec: fakeExec({ claude: false }), env }).mode, 'copy');
});

test('marketplace add succeeds but plugin install fails: marketplace is removed and state is consistent', () => {
  const { env, root } = makeEnv();
  const exec = fakeExec({ claudeFailsOn: ['plugin install'] });
  const r = install({ exec, env });
  const lines = exec.lines();
  const addIdx = lines.findIndex((l) => l.includes('plugin marketplace add'));
  const instIdx = lines.findIndex((l) => l.includes('plugin install'));
  assert.ok(lines.slice(instIdx + 1).some((l) => l.includes('plugin marketplace remove code-check')));
  assert.ok(addIdx < instIdx);
  assert.equal(r.mode, 'copy');
  const receipt = JSON.parse(fs.readFileSync(path.join(root, 'data', 'receipt.json'), 'utf8'));
  assert.equal(receipt.marketplaceAdded, false);
});

test('refuses to replace an unowned <data>/plugin directory', () => {
  const { env, root } = makeEnv();
  fs.mkdirSync(path.join(root, 'data', 'plugin'), { recursive: true });
  fs.writeFileSync(path.join(root, 'data', 'plugin', 'precious'), 'x');
  assert.throws(() => install({ exec: fakeExec(), env }), /refusing to replace/);
  assert.ok(fs.existsSync(path.join(root, 'data', 'plugin', 'precious')));
});

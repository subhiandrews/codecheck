import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { install } from '../installer/lib/install.js';
import { uninstall } from '../installer/lib/uninstall.js';
import { fakeExec, makeEnv } from './helpers.js';

test('plugin mode uninstall unregisters and removes staged files, keeping shadow data', () => {
  const { env, root } = makeEnv();
  install({ exec: fakeExec(), env });
  fs.mkdirSync(path.join(root, 'data', 'shadow'), { recursive: true });
  fs.writeFileSync(path.join(root, 'data', 'shadow', 'run.jsonl'), '{}');
  const exec = fakeExec();
  uninstall({ exec, env });
  const lines = exec.lines();
  assert.ok(lines.some((l) => l.includes('plugin uninstall code-check@code-check')));
  assert.ok(lines.some((l) => l.includes('plugin marketplace remove code-check')));
  assert.equal(fs.existsSync(path.join(root, 'data', 'plugin')), false);
  assert.equal(fs.existsSync(path.join(root, 'data', 'receipt.json')), false);
  assert.ok(fs.existsSync(path.join(root, 'data', 'shadow', 'run.jsonl')));
});

test('copy mode uninstall removes only what was installed', () => {
  const { env, root } = makeEnv();
  const foreign = path.join(env.CLAUDE_CONFIG_DIR, 'skills', 'other-skill');
  fs.mkdirSync(foreign, { recursive: true });
  fs.writeFileSync(path.join(foreign, 'SKILL.md'), 'mine');
  install({ exec: fakeExec({ claude: false }), env });
  uninstall({ exec: fakeExec({ claude: false }), env });
  assert.equal(fs.existsSync(path.join(env.CLAUDE_CONFIG_DIR, 'skills', 'review-pr')), false);
  assert.equal(fs.existsSync(path.join(env.CLAUDE_CONFIG_DIR, 'agents', 'architect.md')), false);
  assert.equal(fs.readFileSync(path.join(foreign, 'SKILL.md'), 'utf8'), 'mine');
  assert.equal(fs.existsSync(path.join(root, 'data', 'plugin')), false);
});

test('--purge-data also removes shadow and state; uninstall without a receipt is harmless', () => {
  const { env, root } = makeEnv();
  fs.mkdirSync(path.join(root, 'data', 'shadow'), { recursive: true });
  fs.mkdirSync(path.join(root, 'data', 'state'), { recursive: true });
  uninstall({ exec: fakeExec(), env, purgeData: true });
  assert.equal(fs.existsSync(path.join(root, 'data', 'shadow')), false);
  assert.equal(fs.existsSync(path.join(root, 'data', 'state')), false);
});

test('a tampered receipt cannot delete paths outside the Claude skills/agents dirs', () => {
  const { env, root } = makeEnv();
  install({ exec: fakeExec({ claude: false }), env });
  const victim = path.join(root, 'victim');
  fs.mkdirSync(victim);
  fs.writeFileSync(path.join(victim, 'keep'), 'x');
  const rp = path.join(root, 'data', 'receipt.json');
  const r = JSON.parse(fs.readFileSync(rp, 'utf8'));
  r.files.push(victim, path.join(env.CLAUDE_CONFIG_DIR, 'skills'), path.join(env.CLAUDE_CONFIG_DIR, 'skills', '..', '..', 'victim'));
  fs.writeFileSync(rp, JSON.stringify(r));
  const logs = [];
  uninstall({ exec: fakeExec({ claude: false }), env, log: (m) => logs.push(m) });
  assert.ok(fs.existsSync(path.join(victim, 'keep')));
  assert.ok(fs.existsSync(path.join(env.CLAUDE_CONFIG_DIR, 'skills')));
  assert.ok(logs.some((m) => /refusing/.test(m)));
});

test('uninstall runs claude removals whenever the receipt recorded a marketplace, whatever the mode', () => {
  const { env, root } = makeEnv();
  install({ exec: fakeExec(), env });
  const rp = path.join(root, 'data', 'receipt.json');
  const r = JSON.parse(fs.readFileSync(rp, 'utf8'));
  r.mode = 'copy';
  r.marketplaceAdded = true;
  fs.writeFileSync(rp, JSON.stringify(r));
  const exec = fakeExec();
  uninstall({ exec, env });
  assert.ok(exec.lines().some((l) => l.includes('plugin marketplace remove code-check')));
});

test('uninstall refuses to delete plugin/venv without an ownership marker', () => {
  const { env, root } = makeEnv();
  fs.mkdirSync(path.join(root, 'data', 'venv'), { recursive: true });
  fs.writeFileSync(path.join(root, 'data', 'venv', 'keep'), 'x');
  uninstall({ exec: fakeExec(), env });
  assert.ok(fs.existsSync(path.join(root, 'data', 'venv', 'keep')));
});

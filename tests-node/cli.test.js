import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';

const CLI = new URL('../installer/cli.js', import.meta.url).pathname;
const run = (...args) => spawnSync(process.execPath, [CLI, ...args], { encoding: 'utf8' });

test('usage mentions the provider option', () => {
  const r = run();
  assert.equal(r.status, 0);
  assert.match(r.stdout, /install \[--provider github\|gitlab\|both\]/);
});

test('a bad or missing --provider value is an error for install and doctor, before anything runs', () => {
  for (const args of [['install', '--provider', 'bitbucket'], ['install', '--provider'], ['install', '--provider='],
    ['doctor', '--provider=nope'], ['doctor', '--provider']]) {
    const r = run(...args);
    assert.equal(r.status, 1, args.join(' '));
    assert.match(r.stderr, /--provider must be one of github, gitlab, both/, args.join(' '));
  }
});

test('an unknown command still prints usage and exits 2', () => {
  const r = run('bogus', '--provider', 'gitlab');
  assert.equal(r.status, 2);
});

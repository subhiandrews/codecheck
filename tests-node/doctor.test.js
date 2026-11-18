import test from 'node:test';
import assert from 'node:assert/strict';
import { formatDoctor, parsePython, parseProvider, runDoctor } from '../installer/lib/doctor.js';
import { fakeExec } from './helpers.js';

test('parsePython reads version triples', () => {
  assert.deepEqual(parsePython('Python 3.14.10'), [3, 14, 10]);
  assert.equal(parsePython('nope'), null);
});

test('doctor passes when everything is present', () => {
  const result = runDoctor({ exec: fakeExec() });
  assert.equal(result.ok, true);
  assert.equal(result.checks.find((c) => c.name === 'python').warn, false);
});

test('python older than 3.14.10 warns but does not fail', () => {
  const result = runDoctor({ exec: fakeExec({ python: 'Python 3.14.8' }) });
  const py = result.checks.find((c) => c.name === 'python');
  assert.equal(result.ok, true);
  assert.equal(py.warn, true);
  assert.match(py.detail, /3\.14\.10/);
});

test('python older than 3.14 fails', () => {
  assert.equal(runDoctor({ exec: fakeExec({ python: null }) }).ok, false);
});

test('missing gh fails; missing claude does not', () => {
  assert.equal(runDoctor({ exec: fakeExec({ gh: false }) }).ok, false);
  assert.equal(runDoctor({ exec: fakeExec({ claude: false }) }).ok, true);
});

const names = (provider, opts) => runDoctor({ exec: fakeExec(opts), provider }).checks.map((c) => c.name);

test('the default checks are exactly the GitHub ones and never touch glab', () => {
  const exec = fakeExec();
  const result = runDoctor({ exec });
  assert.deepEqual(result.checks.map((c) => c.name), ['python', 'git', 'gh', 'gh auth', 'claude']);
  assert.deepEqual(names('github'), ['python', 'git', 'gh', 'gh auth', 'claude']);
  assert.ok(!exec.lines().some((l) => l.startsWith('glab')));
});

test('gitlab needs glab, not gh', () => {
  assert.deepEqual(names('gitlab'), ['python', 'git', 'glab', 'glab auth', 'claude']);
  assert.equal(runDoctor({ exec: fakeExec({ gh: false }), provider: 'gitlab' }).ok, true);
  assert.equal(runDoctor({ exec: fakeExec({ glab: false }), provider: 'gitlab' }).ok, false);
});

test('both needs gh and glab', () => {
  assert.deepEqual(names('both'), ['python', 'git', 'gh', 'gh auth', 'glab', 'glab auth', 'claude']);
  assert.equal(runDoctor({ exec: fakeExec(), provider: 'both' }).ok, true);
  assert.equal(runDoctor({ exec: fakeExec({ gh: false }), provider: 'both' }).ok, false);
  assert.equal(runDoctor({ exec: fakeExec({ glab: false }), provider: 'both' }).ok, false);
});

test('github still ignores a missing glab', () => {
  assert.equal(runDoctor({ exec: fakeExec({ glab: false }) }).ok, true);
});

test('an unknown provider is rejected before anything runs', () => {
  const exec = fakeExec();
  assert.throws(() => runDoctor({ exec, provider: 'bitbucket' }), /--provider must be one of github, gitlab, both/);
  assert.throws(() => parseProvider(undefined), /got nothing/);
  assert.equal(exec.calls.length, 0);
});

test('formatDoctor marks warnings and failures', () => {
  const text = formatDoctor(runDoctor({ exec: fakeExec({ python: 'Python 3.14.8', gh: false }) }));
  assert.match(text, /\[warn\] python/);
  assert.match(text, /\[FAIL\] gh/);
});

test('a missing host CLI says what to install and which --provider to pass', () => {
  const gh = runDoctor({ exec: fakeExec({ gh: false }) }).checks.find((c) => c.name === 'gh');
  assert.match(gh.detail, /install gh.*--provider gitlab/);
  const glab = runDoctor({ exec: fakeExec({ glab: false }), provider: 'gitlab' }).checks.find((c) => c.name === 'glab');
  assert.match(glab.detail, /install glab.*--provider github/);
});

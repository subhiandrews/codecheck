import test from 'node:test';
import assert from 'node:assert/strict';
import { formatDoctor, parsePython, runDoctor } from '../installer/lib/doctor.js';
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

test('formatDoctor marks warnings and failures', () => {
  const text = formatDoctor(runDoctor({ exec: fakeExec({ python: 'Python 3.14.8', gh: false }) }));
  assert.match(text, /\[warn\] python/);
  assert.match(text, /\[FAIL\] gh/);
});

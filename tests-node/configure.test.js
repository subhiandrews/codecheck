import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { install } from '../installer/lib/install.js';
import { uninstall } from '../installer/lib/uninstall.js';
import { BLOCK_START, parseConfigOptions } from '../installer/lib/configure.js';
import { fakeExec, makeEnv } from './helpers.js';

const cfgPath = (env) => path.join(env.HOME, '.config', 'code-check', 'config.toml');
const tokenFile = (root, text = 'tok-123\n', mode = 0o600) => {
  const f = path.join(root, 'token');
  fs.writeFileSync(f, text, { mode });
  fs.chmodSync(f, mode);
  return f;
};
const opts = (...args) => parseConfigOptions(args);

test('without options the installer leaves the user config alone', () => {
  const { env } = makeEnv();
  assert.equal(parseConfigOptions(['--provider', 'github']), null);
  install({ exec: fakeExec(), env });
  assert.equal(fs.existsSync(cfgPath(env)), false);
});

test('--shadow turns on hand-off recording without telemetry', () => {
  const { env } = makeEnv();
  install({ exec: fakeExec(), env, config: opts('--shadow') });
  const text = fs.readFileSync(cfgPath(env), 'utf8');
  assert.match(text, /\[shadow\]\nhandoff = true/);
  assert.doesNotMatch(text, /telemetry/);
});

test('a remote collector writes the endpoint and the token file path, never the token', () => {
  const { env, root } = makeEnv();
  const f = tokenFile(root);
  install({ exec: fakeExec(), env, config: opts('--telemetry-endpoint', 'https://otel.example.com:4318', '--telemetry-token-file', f, '--telemetry-content') });
  const text = fs.readFileSync(cfgPath(env), 'utf8');
  assert.match(text, /\[telemetry\]\nenabled = true\nendpoint = "https:\/\/otel.example.com:4318"\ntoken_file = /);
  assert.ok(text.includes(JSON.stringify(f)) && /content = true/.test(text) && /handoff = true/.test(text));
  assert.doesNotMatch(text, /tok-123/);
  assert.equal(fs.statSync(cfgPath(env)).mode & 0o077, 0);
});

test('a remote collector without a token is refused before anything is installed', () => {
  const { env, root } = makeEnv();
  const exec = fakeExec();
  assert.throws(() => install({ exec, env, config: opts('--telemetry-endpoint', 'https://otel.example.com') }), /token is required/);
  assert.equal(exec.calls.length, 0);
  assert.equal(fs.existsSync(path.join(root, 'data', 'plugin')), false);
});

test('an authorization header in the environment satisfies the token rule, an empty one does not', () => {
  const { env } = makeEnv();
  const e = { ...env, OTEL_EXPORTER_OTLP_HEADERS: 'Authorization=Bearer%20abc' };
  install({ exec: fakeExec(), env: e, config: opts('--telemetry-endpoint', 'https://otel.example.com') });
  assert.doesNotMatch(fs.readFileSync(cfgPath(env), 'utf8'), /token_file|abc/);
  for (const bad of ['Authorization=', 'Authorization=Bearer%20', 'x-team=core']) {
    assert.throws(() => install({ exec: fakeExec(), env: { ...env, OTEL_EXPORTER_OTLP_HEADERS: bad }, config: opts('--telemetry-endpoint', 'https://otel.example.com') }), /token is required/, bad);
  }
});

test('localhost needs no token, but the endpoint must still be safe', () => {
  const { env } = makeEnv();
  install({ exec: fakeExec(), env, config: opts('--telemetry-endpoint', 'http://localhost:4318') });
  assert.match(fs.readFileSync(cfgPath(env), 'utf8'), /endpoint = "http:\/\/localhost:4318"/);
  for (const bad of ['http://otel.example.com', 'https://u:p@otel.example.com', 'ftp://x', 'nope']) {
    assert.throws(() => install({ exec: fakeExec(), env, config: opts('--telemetry-endpoint', bad) }), /--telemetry-endpoint/, bad);
  }
});

test('a token file must be private, non-empty and one line', () => {
  const { env, root } = makeEnv();
  const ep = ['--telemetry-endpoint', 'https://otel.example.com'];
  assert.throws(() => install({ exec: fakeExec(), env, config: opts(...ep, '--telemetry-token-file', tokenFile(root, 'tok', 0o644)) }), /readable by other users/);
  assert.throws(() => install({ exec: fakeExec(), env, config: opts(...ep, '--telemetry-token-file', tokenFile(root, '  \n')) }), /non-empty/);
  assert.throws(() => install({ exec: fakeExec(), env, config: opts(...ep, '--telemetry-token-file', tokenFile(root, 'a b')) }), /single line/);
  assert.throws(() => install({ exec: fakeExec(), env, config: opts(...ep, '--telemetry-token-file', path.join(root, 'nope')) }), /cannot be read/);
});

test('token and content flags need an endpoint', () => {
  assert.throws(() => opts('--telemetry-token-file', '/x'), /need --telemetry-endpoint/);
  assert.throws(() => opts('--telemetry-content'), /need --telemetry-endpoint/);
  assert.throws(() => opts('--telemetry-endpoint'), /needs a value/);
});

const seed = (env, text) => {
  fs.mkdirSync(path.dirname(cfgPath(env)), { recursive: true });
  fs.writeFileSync(cfgPath(env), text);
};

test('an existing [shadow] table that already has handoff = true is kept, and only [telemetry] is added', () => {
  const { env } = makeEnv();
  seed(env, '[shadow]\nenabled = true\nhandoff = true  # on\n');
  install({ exec: fakeExec(), env, config: opts('--telemetry-endpoint', 'http://localhost:4318') });
  const text = fs.readFileSync(cfgPath(env), 'utf8');
  assert.equal(text.match(/\[shadow\]/g).length, 1);
  assert.match(text, /\[telemetry\]\nenabled = true/);
  uninstall({ exec: fakeExec(), env });
  assert.equal(fs.readFileSync(cfgPath(env), 'utf8').trim(), '[shadow]\nenabled = true\nhandoff = true  # on');
});

test('--shadow on a file that already has handoff = true changes nothing', () => {
  const { env } = makeEnv();
  seed(env, '[shadow]\nhandoff = true\n');
  const logs = [];
  install({ exec: fakeExec(), env, config: opts('--shadow'), log: (m) => logs.push(m) });
  assert.equal(fs.readFileSync(cfgPath(env), 'utf8'), '[shadow]\nhandoff = true\n');
  assert.ok(logs.some((l) => l.includes('already has the requested settings')));
});

test('a [shadow] table without handoff = true is a conflict, and nothing is changed', () => {
  const { env } = makeEnv();
  for (const text of ['[shadow]\nstore = "/x"\n', '[shadow]\nhandoff = false\n']) {
    seed(env, text);
    assert.throws(() => install({ exec: fakeExec(), env, config: opts('--shadow') }), /\[shadow\] table without `handoff = true`/);
    assert.equal(fs.readFileSync(cfgPath(env), 'utf8'), text);
  }
});

test('an existing [telemetry] table or a sub-table is a conflict', () => {
  const { env } = makeEnv();
  seed(env, '[shadow]\nhandoff = true\n\n[telemetry]\nenabled = false\n');
  assert.throws(() => install({ exec: fakeExec(), env, config: opts('--telemetry-endpoint', 'http://localhost:4318') }), /already has a \[telemetry\] table/);
  seed(env, '[telemetry.extra]\nx = 1\n');
  assert.throws(() => install({ exec: fakeExec(), env, config: opts('--shadow') }), /\[telemetry\.\*\]/);
  assert.equal(fs.readFileSync(cfgPath(env), 'utf8'), '[telemetry.extra]\nx = 1\n');
});

test('re-install replaces the block, and uninstall removes only the block', () => {
  const { env } = makeEnv();
  fs.mkdirSync(path.dirname(cfgPath(env)), { recursive: true });
  fs.writeFileSync(cfgPath(env), '[review]\nsecurity = true\n');
  install({ exec: fakeExec(), env, config: opts('--shadow') });
  install({ exec: fakeExec(), env, config: opts('--telemetry-endpoint', 'http://localhost:4318') });
  const text = fs.readFileSync(cfgPath(env), 'utf8');
  assert.equal(text.split(BLOCK_START).length, 2);
  assert.match(text, /\[review\]\nsecurity = true/);
  assert.match(text, /enabled = true/);
  uninstall({ exec: fakeExec(), env });
  assert.equal(fs.readFileSync(cfgPath(env), 'utf8').trim(), '[review]\nsecurity = true');
});

test('uninstall deletes a config file the installer created', () => {
  const { env } = makeEnv();
  install({ exec: fakeExec(), env, config: opts('--shadow') });
  uninstall({ exec: fakeExec(), env });
  assert.equal(fs.existsSync(cfgPath(env)), false);
});

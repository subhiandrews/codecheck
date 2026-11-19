import fs from 'node:fs';
import path from 'node:path';

// The installer owns one marked block in the user's config file and nothing else in it. The token is never
// written: only the path of a file that holds it (or nothing, when OTEL_EXPORTER_OTLP_HEADERS carries it).
export const BLOCK_START = '# >>> code-check installer >>>';
export const BLOCK_END = '# <<< code-check installer <<<';
const LOOPBACK = new Set(['localhost', '127.0.0.1', '[::1]', '::1']);
const OWN_BLOCK = new RegExp(`\\n?${BLOCK_START}[\\s\\S]*?${BLOCK_END}\\n?`, 'g');
const TABLE = /^\s*\[\s*(shadow|telemetry)\s*[\].]/m;

const toml = (s) => JSON.stringify(s); // a JSON string is a valid TOML basic string

export function stripBlock(text) {
  return text.replace(OWN_BLOCK, '\n').replace(/^\n+/, '');
}

/** Options from the command line: { shadow, endpoint, tokenFile, content }; null when nothing was asked for. */
export function parseConfigOptions(args) {
  const value = (name) => {
    const i = args.findIndex((a) => a === name || a.startsWith(`${name}=`));
    if (i < 0) return undefined;
    const v = args[i].startsWith(`${name}=`) ? args[i].slice(name.length + 1) : args[i + 1];
    if (!v || v.startsWith('--')) throw new Error(`${name} needs a value`);
    return v;
  };
  const opts = {
    shadow: args.includes('--shadow'),
    endpoint: value('--telemetry-endpoint'),
    tokenFile: value('--telemetry-token-file'),
    content: args.includes('--telemetry-content'),
  };
  if (!opts.endpoint && (opts.tokenFile || opts.content)) throw new Error('--telemetry-token-file and --telemetry-content need --telemetry-endpoint');
  return opts.shadow || opts.endpoint ? opts : null;
}

function checkEndpoint(raw) {
  let u;
  try {
    u = new URL(raw);
  } catch {
    throw new Error('--telemetry-endpoint must be an http:// or https:// URL');
  }
  if (!['http:', 'https:'].includes(u.protocol)) throw new Error('--telemetry-endpoint must be an http:// or https:// URL');
  if (u.username || u.password) throw new Error('--telemetry-endpoint must not contain credentials; use --telemetry-token-file');
  const local = LOOPBACK.has(u.hostname);
  if (u.protocol === 'http:' && !local) throw new Error('--telemetry-endpoint must use https:// unless it is localhost');
  return local;
}

function checkTokenFile(file) {
  const p = path.resolve(file);
  let st;
  try {
    st = fs.statSync(p);
  } catch {
    throw new Error(`--telemetry-token-file ${p} cannot be read`);
  }
  if (!st.isFile()) throw new Error('--telemetry-token-file must be a regular file');
  if (st.mode & 0o077) throw new Error(`--telemetry-token-file ${p} is readable by other users: chmod 600 it`);
  const token = fs.readFileSync(p, 'utf8').trim();
  if (!token || /\s/.test(token)) throw new Error('--telemetry-token-file must hold one non-empty token on a single line');
  return p;
}

const hasHeaderToken = (env) =>
  [env.OTEL_EXPORTER_OTLP_TRACES_HEADERS, env.OTEL_EXPORTER_OTLP_HEADERS].some((h) => /authorization\s*=\s*(bearer|basic)?(%20|\s)*[^\s,]+/i.test(h || '') && !/authorization\s*=\s*(bearer|basic)?(%20|\s)*(,|$)/i.test(h || ''));

/** Validate before the install changes anything; returns the block text to write, or null. */
export function planConfig(opts, paths, env) {
  if (!opts) return null;
  const lines = [BLOCK_START, '[shadow]', 'handoff = true', ''];
  if (opts.endpoint) {
    const local = checkEndpoint(opts.endpoint);
    const tokenFile = opts.tokenFile ? checkTokenFile(opts.tokenFile) : null;
    if (!local && !tokenFile && !hasHeaderToken(env)) {
      throw new Error('a token is required for a remote collector: pass --telemetry-token-file <path> (a file only you can read) or set Authorization in OTEL_EXPORTER_OTLP_HEADERS');
    }
    lines.push('[telemetry]', 'enabled = true', `endpoint = ${toml(opts.endpoint)}`);
    if (tokenFile) lines.push(`token_file = ${toml(tokenFile)}`);
    if (opts.content) lines.push('content = true');
    lines.push('');
  }
  lines.push(BLOCK_END, '');
  const existing = fs.existsSync(paths.configFile) ? stripBlock(fs.readFileSync(paths.configFile, 'utf8')) : '';
  if (TABLE.test(existing)) {
    throw new Error(`${paths.configFile} already has a [shadow] or [telemetry] table; edit it by hand instead (TOML cannot repeat a table)`);
  }
  return lines.join('\n');
}

/** Write the block (replacing a previous installer block). Returns whether the file did not exist before. */
export function writeConfig(block, paths) {
  const existed = fs.existsSync(paths.configFile);
  const existing = existed ? stripBlock(fs.readFileSync(paths.configFile, 'utf8')) : '';
  fs.mkdirSync(path.dirname(paths.configFile), { recursive: true });
  fs.writeFileSync(paths.configFile, `${existing}${existing && !existing.endsWith('\n') ? '\n' : ''}${existing ? '\n' : ''}${block}`, { mode: 0o600 });
  return !existed;
}

/** Remove the installer's block; delete the file when the installer created it and nothing else is left. */
export function removeConfig(paths, created) {
  if (!fs.existsSync(paths.configFile)) return;
  const text = fs.readFileSync(paths.configFile, 'utf8');
  if (!text.includes(BLOCK_START)) return;
  const rest = stripBlock(text);
  if (created && !rest.trim()) fs.rmSync(paths.configFile, { force: true });
  else fs.writeFileSync(paths.configFile, rest);
}

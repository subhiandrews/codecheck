const RECOMMENDED = [3, 14, 10];
const FLOOR = [3, 14, 0];
const cmp = (a, b) => {
  for (let i = 0; i < 3; i += 1) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
};

export function parsePython(text) {
  const m = /Python (\d+)\.(\d+)\.(\d+)/.exec(text || '');
  return m ? m.slice(1).map(Number) : null;
}

export function findPython(exec) {
  for (const cmd of ['python3.14', 'python3', 'python']) {
    const r = exec(cmd, ['--version']);
    const version = r.status === 0 ? parsePython(r.stdout || r.stderr) : null;
    if (version && cmp(version, FLOOR) >= 0) return { cmd, version };
  }
  return null;
}

export const PROVIDERS = ['github', 'gitlab', 'both'];

export function parseProvider(value) {
  if (!PROVIDERS.includes(value)) throw new Error(`--provider must be one of ${PROVIDERS.join(', ')} (got ${value === undefined ? 'nothing' : JSON.stringify(value)})`);
  return value;
}

// The code host CLIs a provider choice needs: [check name, command, args, required].
const HOST_CHECKS = {
  github: [['gh', 'gh', ['--version'], true], ['gh auth', 'gh', ['auth', 'status'], false]],
  gitlab: [['glab', 'glab', ['--version'], true], ['glab auth', 'glab', ['auth', 'status'], false]],
};
// Shown instead of the raw "spawnSync ... ENOENT" when a host CLI is not installed.
const NOT_FOUND_HINT = {
  gh: 'not found (install gh, or pass --provider gitlab if you only use GitLab)',
  glab: 'not found (install glab, or pass --provider github if you only use GitHub)',
};
const hostChecks = (provider) => (provider === 'both' ? [...HOST_CHECKS.github, ...HOST_CHECKS.gitlab] : HOST_CHECKS[provider]);

export function runDoctor({ exec, provider = 'github' }) {
  const checks = [];
  const hosts = hostChecks(parseProvider(provider));
  const py = findPython(exec);
  if (py) {
    const warn = cmp(py.version, RECOMMENDED) < 0;
    checks.push({
      name: 'python', ok: true, required: true, warn,
      detail: `${py.cmd} ${py.version.join('.')}` + (warn ? ` (upgrade to ${RECOMMENDED.join('.')} or later for security fixes)` : ''),
    });
  } else {
    checks.push({ name: 'python', ok: false, required: true, warn: false, detail: 'Python >= 3.14 not found (3.14.10 or later recommended)' });
  }
  for (const [name, cmd, args, required] of [
    ['git', 'git', ['--version'], true],
    ...hosts,
    ['claude', 'claude', ['--version'], false],
  ]) {
    const r = exec(cmd, args);
    const detail = r.status === 127 && NOT_FOUND_HINT[cmd] ? NOT_FOUND_HINT[cmd] : ((r.stdout || r.stderr).trim().split('\n')[0]) || '';
    checks.push({ name, ok: r.status === 0, required, warn: false, detail });
  }
  return { ok: checks.every((c) => c.ok || !c.required), checks };
}

export function formatDoctor(result) {
  return result.checks
    .map((c) => {
      const tag = c.ok ? (c.warn ? '[warn]' : '[ ok ]') : c.required ? '[FAIL]' : '[skip]';
      return `${tag} ${c.name}${c.detail ? `: ${c.detail}` : ''}`;
    })
    .join('\n');
}

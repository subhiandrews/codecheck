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

export function runDoctor({ exec }) {
  const checks = [];
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
  const probe = (name, cmd, args, required) => {
    const r = exec(cmd, args);
    return { name, ok: r.status === 0, required, warn: false, detail: ((r.stdout || r.stderr).trim().split('\n')[0]) || '' };
  };
  checks.push(probe('git', 'git', ['--version'], true));
  // One code-host CLI is enough: gh for GitHub, glab for GitLab. Each is optional while the other is present.
  const hosts = ['gh', 'glab'].map((cmd) => probe(cmd, cmd, ['--version'], false));
  const noHost = hosts.every((h) => !h.ok);
  for (const h of hosts) {
    checks.push({ ...h, required: noHost, detail: noHost ? 'not found (install gh for GitHub or glab for GitLab)' : h.detail });
    if (h.ok) checks.push(probe(`${h.name} auth`, h.name, ['auth', 'status'], false));
  }
  checks.push(probe('claude', 'claude', ['--version'], false));
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

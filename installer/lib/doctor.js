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
  for (const [name, cmd, args, required] of [
    ['git', 'git', ['--version'], true],
    ['gh', 'gh', ['--version'], true],
    ['gh auth', 'gh', ['auth', 'status'], false],
    ['claude', 'claude', ['--version'], false],
  ]) {
    const r = exec(cmd, args);
    checks.push({ name, ok: r.status === 0, required, warn: false, detail: ((r.stdout || r.stderr).trim().split('\n')[0]) || '' });
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

import { spawnSync } from 'node:child_process';

// Always spawns with an argument array (never a shell).
export function realExec(cmd, args = [], opts = {}) {
  const r = spawnSync(cmd, args, { encoding: 'utf8', ...opts, shell: false });
  if (r.error) return { status: r.error.code === 'ENOENT' ? 127 : 1, stdout: '', stderr: String(r.error.message) };
  return { status: r.status ?? 1, stdout: r.stdout || '', stderr: r.stderr || '' };
}

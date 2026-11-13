import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const roots = [];
process.on('exit', () => {
  for (const r of roots) fs.rmSync(r, { recursive: true, force: true });
});

export function makeEnv() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'code-check-'));
  roots.push(root);
  const home = path.join(root, 'home');
  fs.mkdirSync(home, { recursive: true });
  return {
    root,
    env: { ...process.env, HOME: home, CODE_CHECK_HOME: path.join(root, 'data'), CLAUDE_CONFIG_DIR: path.join(home, '.claude') },
  };
}

/** Scripted exec: records every call; flags choose which tools "exist" or fail. */
export function fakeExec({ python = 'Python 3.14.10', gh = true, claude = true, claudeFailsOn = [] } = {}) {
  const calls = [];
  const exec = (cmd, args = [], opts = {}) => {
    calls.push({ cmd, args, opts });
    const line = [cmd, ...args].join(' ');
    if (/^python/.test(cmd)) return cmd === 'python3.14' && python ? { status: 0, stdout: `${python}\n`, stderr: '' } : { status: 127, stdout: '', stderr: 'not found' };
    if (cmd === 'gh') return gh ? { status: 0, stdout: 'gh version 2.60\n', stderr: '' } : { status: 127, stdout: '', stderr: 'not found' };
    if (cmd === 'claude') {
      if (!claude) return { status: 127, stdout: '', stderr: 'not found' };
      return claudeFailsOn.some((s) => line.includes(s)) ? { status: 1, stdout: '', stderr: 'boom' } : { status: 0, stdout: '1.0.0\n', stderr: '' };
    }
    return { status: 0, stdout: 'ok\n', stderr: '' };
  };
  exec.calls = calls;
  exec.lines = () => calls.map((c) => [c.cmd, ...c.args].join(' '));
  return exec;
}

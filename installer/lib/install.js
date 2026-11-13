import fs from 'node:fs';
import path from 'node:path';
import { runDoctor } from './doctor.js';
import { realExec } from './exec.js';
import { resolvePaths } from './paths.js';

const PAYLOAD = ['.claude-plugin', 'hooks', 'bin', 'skills', 'agents', 'src', 'pyproject.toml', 'VERSION', 'README.md'];

// Spellings recorded by the Task 1 spike.
export const CLAUDE_CMDS = {
  marketplaceAdd: (dir) => ['plugin', 'marketplace', 'add', dir],
  marketplaceRemove: ['plugin', 'marketplace', 'remove', 'code-check'],
  install: ['plugin', 'install', 'code-check@code-check'],
  uninstall: ['plugin', 'uninstall', 'code-check@code-check'],
};

export const stageDir = (paths) => path.join(paths.dataDir, 'plugin');

function copyPayload(src, dest) {
  fs.rmSync(dest, { recursive: true, force: true });
  fs.mkdirSync(dest, { recursive: true });
  for (const item of PAYLOAD) {
    const from = path.join(src, item);
    if (fs.existsSync(from)) {
      fs.cpSync(from, path.join(dest, item), {
        recursive: true,
        filter: (p) => !p.includes('__pycache__') && !p.endsWith('.egg-info'),
      });
    }
  }
  for (const f of ['bin/code-check', 'hooks/ensure-runtime.sh']) {
    const p = path.join(dest, f);
    if (fs.existsSync(p)) fs.chmodSync(p, 0o755);
  }
}

function copyMode(paths, stage) {
  const skillsRoot = path.join(paths.claudeDir, 'skills');
  const agentsRoot = path.join(paths.claudeDir, 'agents');
  fs.mkdirSync(skillsRoot, { recursive: true });
  fs.mkdirSync(agentsRoot, { recursive: true });
  const files = [];
  for (const name of fs.readdirSync(path.join(stage, 'skills'))) {
    const dest = path.join(skillsRoot, name);
    fs.rmSync(dest, { recursive: true, force: true });
    fs.mkdirSync(dest, { recursive: true });
    const text = fs
      .readFileSync(path.join(stage, 'skills', name, 'SKILL.md'), 'utf8')
      .replaceAll('${CLAUDE_PLUGIN_ROOT}', stage)
      .replaceAll('code-check:', '');
    fs.writeFileSync(path.join(dest, 'SKILL.md'), text);
    files.push(dest);
  }
  for (const f of fs.readdirSync(path.join(stage, 'agents'))) {
    const dest = path.join(agentsRoot, f);
    fs.copyFileSync(path.join(stage, 'agents', f), dest);
    files.push(dest);
  }
  return files;
}

export function install({ exec = realExec, env = process.env, log = () => {} } = {}) {
  const paths = resolvePaths(env);
  const doctor = runDoctor({ exec });
  const failed = doctor.checks.filter((c) => c.required && !c.ok);
  if (failed.length) throw new Error(`prerequisites missing: ${failed.map((c) => `${c.name} (${c.detail})`).join('; ')}`);
  for (const c of doctor.checks.filter((x) => x.warn)) log(`warning: ${c.name}: ${c.detail}`);

  const stage = stageDir(paths);
  copyPayload(paths.pluginSrc, stage);
  const childEnv = { ...env, CODE_CHECK_HOME: paths.dataDir };
  const runtime = exec('sh', [path.join(stage, 'hooks', 'ensure-runtime.sh')], { env: childEnv });
  if (runtime.status !== 0) throw new Error(`runtime setup failed: ${(runtime.stderr || '').trim()}`);
  const gen = exec(path.join(paths.dataDir, 'venv', 'bin', 'python'), ['-m', 'codecheck', 'gen-agents', '--out', path.join(stage, 'agents')], { env: childEnv });
  if (gen.status !== 0) log('warning: could not apply configured models to the agents; using defaults');

  let mode = 'copy';
  let files = [];
  if (doctor.checks.find((c) => c.name === 'claude')?.ok) {
    exec('claude', CLAUDE_CMDS.uninstall);
    exec('claude', CLAUDE_CMDS.marketplaceRemove);
    const add = exec('claude', CLAUDE_CMDS.marketplaceAdd(stage));
    const done = add.status === 0 ? exec('claude', CLAUDE_CMDS.install) : add;
    if (done.status === 0) mode = 'plugin';
    else log(`claude plugin commands failed; copying skills and agents into ${paths.claudeDir} instead`);
  } else {
    log(`claude not found on PATH; copying skills and agents into ${paths.claudeDir}`);
  }
  if (mode === 'copy') files = copyMode(paths, stage);

  const version = fs.readFileSync(path.join(stage, 'VERSION'), 'utf8').trim();
  fs.writeFileSync(path.join(paths.dataDir, 'receipt.json'), JSON.stringify({ version, mode, stage, files }, null, 2));
  return { mode, stage, files, version };
}

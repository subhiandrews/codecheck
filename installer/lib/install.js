import fs from 'node:fs';
import path from 'node:path';
import { runDoctor } from './doctor.js';
import { realExec } from './exec.js';
import { resolvePaths } from './paths.js';
import { CLAUDE_CMDS } from './commands.js';
import { OWNER_MARKER, isOwned, readReceipt, reverseReceipt } from './uninstall.js';

export { CLAUDE_CMDS };

const PAYLOAD = ['.claude-plugin', 'hooks', 'bin', 'skills', 'agents', 'src', 'pyproject.toml', 'VERSION', 'README.md'];

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

const exists = (p) => {
  try {
    fs.lstatSync(p); // lstat: a dangling or live symlink at the destination counts as a collision
    return true;
  } catch {
    return false;
  }
};

// Never clobbers anything the installer did not create (any entry still present after the
// previous receipt was reversed is foreign). Returns only entries actually created.
function copyMode(paths, stage, log) {
  const skillsRoot = path.join(paths.claudeDir, 'skills');
  const agentsRoot = path.join(paths.claudeDir, 'agents');
  fs.mkdirSync(skillsRoot, { recursive: true });
  fs.mkdirSync(agentsRoot, { recursive: true });
  const files = [];
  const skipped = [];
  const skip = (dest) => {
    skipped.push(dest);
    log(`warning: ${dest} already exists and is not managed by code-check; leaving it untouched and skipping`);
  };
  for (const name of fs.readdirSync(path.join(stage, 'skills'))) {
    const dest = path.join(skillsRoot, name);
    if (exists(dest)) { skip(dest); continue; }
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
    if (exists(dest)) { skip(dest); continue; }
    fs.copyFileSync(path.join(stage, 'agents', f), dest, fs.constants.COPYFILE_EXCL);
    files.push(dest);
  }
  return { files, skipped };
}

export function install({ exec = realExec, env = process.env, log = () => {} } = {}) {
  const paths = resolvePaths(env);
  const doctor = runDoctor({ exec });
  const failed = doctor.checks.filter((c) => c.required && !c.ok);
  if (failed.length) throw new Error(`prerequisites missing: ${failed.map((c) => `${c.name} (${c.detail})`).join('; ')}`);
  for (const c of doctor.checks.filter((x) => x.warn)) log(`warning: ${c.name}: ${c.detail}`);

  const stage = stageDir(paths);
  // Reverse any previous install first (registrations and copied files), so a re-install is
  // idempotent and a mode switch leaves nothing behind. Tolerates a missing or corrupt receipt.
  const previous = readReceipt(paths);
  if (previous) reverseReceipt({ paths, exec, receipt: previous, log });
  if (fs.existsSync(stage) && !isOwned(paths, previous)) {
    throw new Error(`refusing to replace ${stage}: not created by code-check (no ownership marker)`);
  }
  fs.mkdirSync(paths.dataDir, { recursive: true });
  fs.writeFileSync(path.join(paths.dataDir, OWNER_MARKER), 'created by the code-check installer\n');
  copyPayload(paths.pluginSrc, stage);
  const childEnv = { ...env, CODE_CHECK_HOME: paths.dataDir };
  const runtime = exec('sh', [path.join(stage, 'hooks', 'ensure-runtime.sh')], { env: childEnv });
  if (runtime.status !== 0) throw new Error(`runtime setup failed: ${(runtime.stderr || '').trim()}`);
  const gen = exec(path.join(paths.dataDir, 'venv', 'bin', 'python'), ['-m', 'codecheck', 'gen-agents', '--out', path.join(stage, 'agents')], { env: childEnv });
  if (gen.status !== 0) log('warning: could not apply configured models to the agents; using defaults');

  let mode = 'copy';
  let files = [];
  let skipped = [];
  let marketplaceAdded = false;
  if (doctor.checks.find((c) => c.name === 'claude')?.ok) {
    exec('claude', CLAUDE_CMDS.uninstall);
    exec('claude', CLAUDE_CMDS.marketplaceRemove);
    const add = exec('claude', CLAUDE_CMDS.marketplaceAdd(stage));
    marketplaceAdded = add.status === 0;
    const done = marketplaceAdded ? exec('claude', CLAUDE_CMDS.install) : add;
    if (done.status === 0) mode = 'plugin';
    else {
      if (marketplaceAdded) {
        exec('claude', CLAUDE_CMDS.marketplaceRemove);
        marketplaceAdded = false;
      }
      log(`claude plugin commands failed; copying skills and agents into ${paths.claudeDir} instead`);
    }
  } else {
    log(`claude not found on PATH; copying skills and agents into ${paths.claudeDir}`);
  }
  if (mode === 'copy') ({ files, skipped } = copyMode(paths, stage, log));

  const version = fs.readFileSync(path.join(stage, 'VERSION'), 'utf8').trim();
  fs.writeFileSync(path.join(paths.dataDir, 'receipt.json'), JSON.stringify({ version, mode, stage, files, skipped, marketplaceAdded }, null, 2));
  return { mode, stage, files, skipped, version };
}

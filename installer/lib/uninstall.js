import fs from 'node:fs';
import path from 'node:path';
import { realExec } from './exec.js';
import { CLAUDE_CMDS } from './install.js';
import { resolvePaths } from './paths.js';

// A receipt entry may only be removed if it is a direct child of <claudeDir>/skills or <claudeDir>/agents.
function removable(paths, file) {
  if (typeof file !== 'string' || !path.isAbsolute(file)) return false;
  const p = path.resolve(file);
  return ['skills', 'agents'].some((d) => path.dirname(p) === path.join(paths.claudeDir, d));
}

export function uninstall({ exec = realExec, env = process.env, log = () => {}, purgeData = false } = {}) {
  const paths = resolvePaths(env);
  const receiptPath = path.join(paths.dataDir, 'receipt.json');
  let receipt = null;
  try {
    receipt = JSON.parse(fs.readFileSync(receiptPath, 'utf8'));
  } catch {
    log('no install receipt found; removing what can be found');
  }
  if (!receipt || receipt.mode === 'plugin') {
    exec('claude', CLAUDE_CMDS.uninstall);
    exec('claude', CLAUDE_CMDS.marketplaceRemove);
  }
  for (const f of Array.isArray(receipt?.files) ? receipt.files : []) {
    if (removable(paths, f)) fs.rmSync(path.resolve(f), { recursive: true, force: true });
    else log(`refusing to remove ${String(f)}: outside ${paths.claudeDir}/skills and agents`);
  }
  for (const d of ['plugin', 'venv']) fs.rmSync(path.join(paths.dataDir, d), { recursive: true, force: true });
  for (const f of ['receipt.json', 'installed-version']) fs.rmSync(path.join(paths.dataDir, f), { force: true });
  if (purgeData) for (const d of ['shadow', 'state']) fs.rmSync(path.join(paths.dataDir, d), { recursive: true, force: true });
  return { mode: receipt?.mode ?? 'unknown' };
}

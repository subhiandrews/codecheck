import fs from 'node:fs';
import path from 'node:path';
import { CLAUDE_CMDS } from './commands.js';
import { realExec } from './exec.js';
import { resolvePaths } from './paths.js';

export const OWNER_MARKER = '.code-check-owned';

// A receipt entry may only be removed if it is a direct child of <claudeDir>/skills or <claudeDir>/agents.
function removable(paths, file) {
  if (typeof file !== 'string' || !path.isAbsolute(file)) return false;
  const p = path.resolve(file);
  return ['skills', 'agents'].some((d) => path.dirname(p) === path.join(paths.claudeDir, d));
}

export function readReceipt(paths) {
  try {
    const r = JSON.parse(fs.readFileSync(path.join(paths.dataDir, 'receipt.json'), 'utf8'));
    return r && typeof r === 'object' ? r : null;
  } catch {
    return null;
  }
}

/** True when the data dir carries the installer's marker, or a receipt names this exact stage. */
export function isOwned(paths, receipt) {
  return (
    fs.existsSync(path.join(paths.dataDir, OWNER_MARKER)) ||
    (typeof receipt?.stage === 'string' && path.resolve(receipt.stage) === path.join(paths.dataDir, 'plugin'))
  );
}

/** Undo what a receipt recorded: claude registrations (any mode, if a marketplace was added) and copied files. */
export function reverseReceipt({ paths, exec, receipt, log }) {
  if (receipt.mode === 'plugin' || receipt.marketplaceAdded) {
    exec('claude', CLAUDE_CMDS.uninstall);
    exec('claude', CLAUDE_CMDS.marketplaceRemove);
  }
  for (const f of Array.isArray(receipt.files) ? receipt.files : []) {
    if (removable(paths, f)) fs.rmSync(path.resolve(f), { recursive: true, force: true });
    else log(`refusing to remove ${String(f)}: outside ${paths.claudeDir}/skills and agents`);
  }
}

export function uninstall({ exec = realExec, env = process.env, log = () => {}, purgeData = false } = {}) {
  const paths = resolvePaths(env);
  const receipt = readReceipt(paths);
  if (!receipt) {
    log('no install receipt found; removing what can be found');
    exec('claude', CLAUDE_CMDS.uninstall);
    exec('claude', CLAUDE_CMDS.marketplaceRemove);
  } else {
    reverseReceipt({ paths, exec, receipt, log });
  }
  if (isOwned(paths, receipt)) {
    for (const d of ['plugin', 'venv']) fs.rmSync(path.join(paths.dataDir, d), { recursive: true, force: true });
    for (const f of ['receipt.json', 'installed-version', OWNER_MARKER]) fs.rmSync(path.join(paths.dataDir, f), { force: true });
  } else {
    log(`refusing to remove plugin/venv under ${paths.dataDir}: no ownership marker`);
  }
  if (purgeData) for (const d of ['shadow', 'state']) fs.rmSync(path.join(paths.dataDir, d), { recursive: true, force: true });
  return { mode: receipt?.mode ?? 'unknown' };
}

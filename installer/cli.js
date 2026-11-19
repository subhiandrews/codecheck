#!/usr/bin/env node
import { formatDoctor, parseProvider, parseTarget, runDoctor } from './lib/doctor.js';
import { realExec } from './lib/exec.js';
import { install } from './lib/install.js';
import { uninstall } from './lib/uninstall.js';

const [cmd, ...rest] = process.argv.slice(2);
const log = (m) => console.log(m);

// `--provider github|gitlab|both` or `--provider=...`; GitHub when it is absent.
function providerFrom(args) {
  const i = args.findIndex((a) => a === '--provider' || a.startsWith('--provider='));
  if (i < 0) return 'github';
  return parseProvider(args[i].startsWith('--provider=') ? args[i].slice('--provider='.length) : args[i + 1]);
}

function targetFrom(args) {
  const i = args.findIndex((a) => a === '--target' || a.startsWith('--target='));
  if (i < 0) return 'claude';
  return parseTarget(args[i].startsWith('--target=') ? args[i].slice('--target='.length) : args[i + 1]);
}

try {
  if (cmd === 'install') {
    const r = install({ log, provider: providerFrom(rest), target: targetFrom(rest) });
    log(`code-check ${r.version} installed (${r.mode} mode${r.provider === 'github' ? '' : `, ${r.provider}`}). Restart ${r.mode === 'codex' ? 'Codex' : 'Claude Code'} to load it.`);
  } else if (cmd === 'uninstall') {
    const r = uninstall({ log, purgeData: rest.includes('--purge-data'), target: targetFrom(rest) });
    log(`code-check uninstalled (${r.mode} mode).`);
  } else if (cmd === 'doctor') {
    const r = runDoctor({ exec: realExec, provider: providerFrom(rest), target: targetFrom(rest) });
    log(formatDoctor(r));
    process.exit(r.ok ? 0 : 1);
  } else {
    log('usage: npx code-check install [--provider github|gitlab|both] [--target claude|codex] | uninstall [--purge-data] [--target claude|codex] | doctor [--provider github|gitlab|both] [--target claude|codex]');
    process.exit(cmd ? 2 : 0);
  }
} catch (e) {
  console.error(`code-check: ${e.message}`);
  process.exit(1);
}

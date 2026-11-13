#!/usr/bin/env node
import { formatDoctor, runDoctor } from './lib/doctor.js';
import { realExec } from './lib/exec.js';
import { install } from './lib/install.js';
import { uninstall } from './lib/uninstall.js';

const [cmd, ...rest] = process.argv.slice(2);
const log = (m) => console.log(m);

try {
  if (cmd === 'install') {
    const r = install({ log });
    log(`code-check ${r.version} installed (${r.mode} mode). Restart Claude Code to load it.`);
  } else if (cmd === 'uninstall') {
    const r = uninstall({ log, purgeData: rest.includes('--purge-data') });
    log(`code-check uninstalled (${r.mode} mode).`);
  } else if (cmd === 'doctor') {
    const r = runDoctor({ exec: realExec });
    log(formatDoctor(r));
    process.exit(r.ok ? 0 : 1);
  } else {
    log('usage: npx code-check install | uninstall [--purge-data] | doctor');
    process.exit(cmd ? 2 : 0);
  }
} catch (e) {
  console.error(`code-check: ${e.message}`);
  process.exit(1);
}

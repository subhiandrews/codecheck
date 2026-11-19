import fs from 'node:fs';
import path from 'node:path';
import { resolvePaths } from './paths.js';

const PLACEHOLDER = '@CODE_CHECK_BIN@';
const CODEX_PAYLOAD = ['hooks', 'bin', 'src', 'pyproject.toml', 'VERSION', 'README.md', 'LICENSE', 'NOTICE'];

function locations(env) {
  const paths = resolvePaths(env);
  const data = path.join(paths.dataDir, 'codex');
  return {
    ...paths, data, skills: path.join(paths.home, '.agents', 'skills'),
    stage: path.join(data, 'runtime'), receipt: path.join(data, 'receipt.json'),
  };
}

function stat(file) {
  try {
    return fs.lstatSync(file);
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function checkSkillRoot(paths) {
  for (const dir of [path.dirname(paths.skills), paths.skills]) {
    const info = stat(dir);
    if (info && !info.isDirectory()) throw new Error(`refusing to use ${dir}: expected a directory without symlinks`);
  }
}

function readReceipt(paths) {
  const info = stat(paths.receipt);
  if (!info) return null;
  if (!info.isFile()) throw new Error('expected a regular Codex install receipt');
  let receipt;
  try {
    receipt = JSON.parse(fs.readFileSync(paths.receipt, 'utf8'));
  } catch {
    throw new Error(`corrupt Codex install receipt at ${paths.receipt}; remove it and the skills it lists, then reinstall`);
  }
  if (receipt?.mode !== 'codex' || !Array.isArray(receipt.files)) throw new Error('invalid Codex install receipt');
  return receipt;
}

function removeSkills(paths, files, log) {
  checkSkillRoot(paths);
  for (const file of files) {
    const dir = typeof file === 'string' && path.isAbsolute(file) ? path.dirname(path.resolve(file)) : null;
    if (dir === null || path.dirname(dir) !== paths.skills || path.basename(file) !== 'SKILL.md'
        || file !== path.join(dir, 'SKILL.md') || stat(dir)?.isSymbolicLink()) {
      log(`refusing to remove ${String(file)}: outside ${paths.skills} or uses a symlink`);
      continue;
    }
    const info = stat(file);
    if (!info) continue;
    if (!info.isFile()) {
      log(`refusing to remove ${file}: expected a regular file`);
      continue;
    }
    fs.unlinkSync(file);
    try {
      fs.rmdirSync(dir);
    } catch (error) {
      if (error.code !== 'ENOTEMPTY') throw error;
    }
  }
}

export function installCodex({ env, log, provider, copyPayload }) {
  const paths = locations(env);
  checkSkillRoot(paths);
  const bin = path.join(paths.stage, 'bin', 'code-check');
  if (/[\r\n]/.test(bin)) throw new Error('Codex install path must fit on one line');
  // Templates place the placeholder inside double quotes in a shell command.
  const escaped = bin.replace(/[\\`"$]/g, (char) => '\\' + char);
  const source = path.join(paths.pluginSrc, 'codex', 'skills');
  const templates = fs.readdirSync(source, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map(({ name }) => {
      const text = fs.readFileSync(path.join(source, name, 'SKILL.md'), 'utf8');
      const rendered = text.replaceAll(PLACEHOLDER, () => escaped);
      if (rendered === text || rendered.includes(PLACEHOLDER)) throw new Error(`unresolved CLI placeholder in ${name}/SKILL.md`);
      return { name, rendered };
    });
  if (!templates.length) throw new Error('no Codex skill templates found');

  fs.mkdirSync(paths.data, { recursive: true });
  const marker = path.join(paths.data, '.code-check-codex-owned');
  const ownership = stat(marker);
  if ((ownership && !ownership.isFile()) || (stat(paths.stage) && !ownership)) {
    throw new Error(`refusing to replace ${paths.stage}: no Codex ownership marker`);
  }
  const previous = readReceipt(paths);
  if (!ownership) fs.writeFileSync(marker, 'created by the code-check Codex installer\n', { flag: 'wx' });
  copyPayload(paths.pluginSrc, paths.stage, CODEX_PAYLOAD);
  const version = fs.readFileSync(path.join(paths.stage, 'VERSION'), 'utf8').trim();

  if (previous) {
    removeSkills(paths, previous.files, log);
    fs.unlinkSync(paths.receipt);
  }
  fs.mkdirSync(paths.skills, { recursive: true });
  const files = [];
  const skipped = [];
  try {
    for (const { name, rendered } of templates) {
      const dir = path.join(paths.skills, name);
      try {
        fs.mkdirSync(dir);
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        skipped.push(dir);
        log(`warning: ${dir} already exists; leaving it untouched and skipping`);
        continue;
      }
      const dest = path.join(dir, 'SKILL.md');
      try {
        fs.writeFileSync(dest, rendered, { flag: 'wx' });
        files.push(dest);
      } catch (error) {
        if (error.code !== 'EEXIST') {
          fs.rmSync(dest, { force: true });
          fs.rmdirSync(dir);
        }
        throw error;
      }
    }
    const receipt = { version, mode: 'codex', provider, files, skipped };
    const temp = fs.mkdtempSync(path.join(paths.data, 'receipt-'));
    try {
      const pending = path.join(temp, 'receipt.json');
      fs.writeFileSync(pending, JSON.stringify(receipt, null, 2));
      fs.renameSync(pending, paths.receipt);
    } finally {
      fs.rmSync(temp, { recursive: true, force: true });
    }
    return receipt;
  } catch (error) {
    removeSkills(paths, files, log);
    throw error;
  }
}

export function uninstallCodex({ env, log }) {
  const paths = locations(env);
  const receipt = readReceipt(paths);
  if (receipt) {
    removeSkills(paths, receipt.files, log);
    fs.unlinkSync(paths.receipt);
  } else {
    log('no Codex install receipt found');
  }
  return { mode: 'codex' };
}

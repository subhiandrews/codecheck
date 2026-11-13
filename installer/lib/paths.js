import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

// Always absolute, so a value like "-x" can never be read as an option by a child process.
export function resolvePaths(env = process.env) {
  const home = path.resolve(env.HOME || os.homedir());
  return {
    home,
    claudeDir: path.resolve(env.CLAUDE_CONFIG_DIR || path.join(home, '.claude')),
    dataDir: path.resolve(env.CODE_CHECK_HOME || path.join(home, '.local', 'share', 'code-check')),
    pluginSrc: path.resolve(HERE, '..', '..'),
  };
}

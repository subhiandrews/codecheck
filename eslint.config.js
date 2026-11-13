import js from '@eslint/js';
import globals from 'globals';

export default [
  { ignores: ['node_modules/', '.venv/', '.worktrees/', 'dist/', 'build/'] },
  js.configs.recommended,
  { files: ['**/*.js'], languageOptions: { ecmaVersion: 'latest', sourceType: 'module', globals: globals.node } },
];

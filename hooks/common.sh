# Shared by bin/code-check and hooks/ensure-runtime.sh (sourced, not executed).
# Single place where the data directory is resolved.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${CODE_CHECK_HOME:-${CLAUDE_PLUGIN_DATA:-${HOME:?code-check: set HOME, CLAUDE_PLUGIN_DATA or CODE_CHECK_HOME}/.local/share/code-check}}"
VENV="$DATA/venv"

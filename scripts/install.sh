#!/usr/bin/env bash
set -euo pipefail

version="${CLAWPATCH_SUPERVISE_VERSION:-0.1.11}"
source_package="${CLAWPATCH_SUPERVISE_SOURCE:-https://github.com/uncmatteth/clawpatch-supervise/releases/download/v${version}/clawpatch_supervise-${version}-py3-none-any.whl}"
install_root="${CLAWPATCH_SUPERVISE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/clawpatch-supervise}"
bin_dir="${CLAWPATCH_SUPERVISE_BIN_DIR:-$HOME/.local/bin}"
python_command="${CLAWPATCH_SUPERVISE_PYTHON:-python3}"
readonly clawpatch_version="0.7.2"
readonly clawhub_version="0.19.1"

command -v "$python_command" >/dev/null 2>&1 || {
  echo "Python 3.11 or newer is required." >&2
  exit 2
}
"$python_command" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || {
  echo "Python 3.11 or newer is required." >&2
  exit 2
}
command -v git >/dev/null 2>&1 || {
  echo "Git is required." >&2
  exit 2
}

if command -v clawhub >/dev/null 2>&1; then
  clawhub_command="$(command -v clawhub)"
else
  command -v npm >/dev/null 2>&1 || {
    echo "ClawHub is missing and npm is unavailable. Install Node.js 22 or newer, then rerun this installer." >&2
    exit 2
  }
  clawhub_command=""
fi

if ! command -v clawpatch >/dev/null 2>&1; then
  command -v npm >/dev/null 2>&1 || {
    echo "npm is required to install ClawPatch." >&2
    exit 2
  }
  npm install --global "clawpatch@${clawpatch_version}"
  npm_global_prefix="$(npm prefix --global)"
  export PATH="$npm_global_prefix/bin:$PATH"
  command -v clawpatch >/dev/null 2>&1 || {
    echo "ClawPatch was installed but its command is not on PATH." >&2
    exit 2
  }
fi

"$python_command" -m venv "$install_root/venv"
"$install_root/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$source_package"

if [[ -z "$clawhub_command" ]]; then
  clawhub_root="$install_root/clawhub"
  echo "ClawHub is missing; installing clawhub@${clawhub_version} into $clawhub_root"
  npm install --prefix "$clawhub_root" --no-fund --no-audit "clawhub@${clawhub_version}"
  clawhub_command="$clawhub_root/node_modules/.bin/clawhub"
  test -x "$clawhub_command" || {
    echo "ClawHub installation did not create its command." >&2
    exit 2
  }
fi

"$install_root/venv/bin/clawpatch-supervise" --version
"$clawhub_command" --cli-version
clawpatch --version

mkdir -p "$bin_dir"
ln -sfn "$install_root/venv/bin/clawpatch-supervise" "$bin_dir/clawpatch-supervise"
if [[ "$clawhub_command" == "$install_root/clawhub/"* ]]; then
  ln -sfn "$clawhub_command" "$bin_dir/clawhub"
  clawhub_command="$bin_dir/clawhub"
fi

echo "Installed command: $bin_dir/clawpatch-supervise"
echo "ClawHub command: $clawhub_command"

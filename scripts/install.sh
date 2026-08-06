#!/usr/bin/env bash
set -euo pipefail

version="${CLAWPATCH_SUPERVISE_VERSION:-0.1.4}"
source_package="${CLAWPATCH_SUPERVISE_SOURCE:-https://github.com/uncmatteth/clawpatch-supervise/releases/download/v${version}/clawpatch_supervise-${version}-py3-none-any.whl}"
install_root="${CLAWPATCH_SUPERVISE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/clawpatch-supervise}"
bin_dir="${CLAWPATCH_SUPERVISE_BIN_DIR:-$HOME/.local/bin}"
python_command="${CLAWPATCH_SUPERVISE_PYTHON:-python3}"

command -v "$python_command" >/dev/null 2>&1 || {
  echo "Python 3.11 or newer is required." >&2
  exit 2
}
command -v git >/dev/null 2>&1 || {
  echo "Git is required." >&2
  exit 2
}

"$python_command" -m venv "$install_root/venv"
"$install_root/venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$source_package"
mkdir -p "$bin_dir"
ln -sfn "$install_root/venv/bin/clawpatch-supervise" "$bin_dir/clawpatch-supervise"

"$bin_dir/clawpatch-supervise" --version
echo "Installed command: $bin_dir/clawpatch-supervise"
if ! command -v clawpatch >/dev/null 2>&1; then
  echo "ClawPatch is not on PATH yet. Install ClawPatch before running a queue." >&2
fi

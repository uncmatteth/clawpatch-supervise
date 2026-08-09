#!/usr/bin/env bash
set -euo pipefail

version="${CLAWPATCH_SUPERVISE_VERSION:-0.1.27}"
source_package="${CLAWPATCH_SUPERVISE_SOURCE:-https://github.com/uncmatteth/clawpatch-supervise/releases/download/v${version}/clawpatch_supervise-${version}-py3-none-any.whl}"
install_root="${CLAWPATCH_SUPERVISE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/clawpatch-supervise}"
bin_dir="${CLAWPATCH_SUPERVISE_BIN_DIR:-$HOME/.local/bin}"
python_command="${CLAWPATCH_SUPERVISE_PYTHON:-python3}"
verify_repo="${CLAWPATCH_SUPERVISE_VERIFY_REPO:-}"
readonly minimum_clawpatch_version="0.7.2"
readonly release_sha256_0_1_27="a6eb22ac8c04d8025fbc7b64b6428586396ba09626eb4fbce1d40a918a22a1c2"
download_root=""
staging_venv=""
pending_supervisor_link=""

cleanup() {
  [[ -z "$pending_supervisor_link" ]] || rm -f "$pending_supervisor_link"
  [[ -z "$staging_venv" ]] || rm -rf "$staging_venv"
  [[ -z "$download_root" ]] || rm -rf "$download_root"
}
trap cleanup EXIT

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

compatible_clawpatch() {
  local command_path="$1"
  local actual_version
  actual_version="$("$command_path" --version 2>/dev/null)" || return 1
  "$python_command" - "$actual_version" "$minimum_clawpatch_version" <<'PY'
import re
import sys

def version(value):
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise SystemExit(1)
    return tuple(map(int, match.groups()))

raise SystemExit(version(sys.argv[1]) < version(sys.argv[2]))
PY
}
command -v node >/dev/null 2>&1 || {
  echo "Node.js 22 or newer is required." >&2
  exit 2
}
node_major="$(node --version | sed -n 's/^v\([0-9][0-9]*\).*/\1/p')"
if [[ -z "$node_major" || "$node_major" -lt 22 ]]; then
  echo "Node.js 22 or newer is required." >&2
  exit 2
fi

if command -v clawpatch >/dev/null 2>&1 && \
  compatible_clawpatch "$(command -v clawpatch)"; then
  clawpatch_command="$(command -v clawpatch)"
else
  command -v npm >/dev/null 2>&1 || {
    echo "npm is required to install ClawPatch." >&2
    exit 2
  }
  clawpatch_root="$install_root/clawpatch"
  npm install --prefix "$clawpatch_root" --no-fund --no-audit "clawpatch@latest"
  clawpatch_command="$clawpatch_root/node_modules/.bin/clawpatch"
  test -x "$clawpatch_command" || {
    echo "ClawPatch installation did not create its command." >&2
    exit 2
  }
fi
if ! compatible_clawpatch "$clawpatch_command"; then
  echo "ClawPatch $minimum_clawpatch_version or newer is required." >&2
  exit 2
fi
clawpatch_installed_version="$("$clawpatch_command" --version)"

package_to_install="$source_package"
if [[ ! -d "$source_package" ]]; then
  expected_sha256="${CLAWPATCH_SUPERVISE_SHA256:-}"
  if [[ -z "$expected_sha256" ]]; then
    if [[ -n "${CLAWPATCH_SUPERVISE_SOURCE:-}" ]]; then
      echo "CLAWPATCH_SUPERVISE_SHA256 is required for a custom wheel source." >&2
      exit 2
    fi
    if [[ "$version" != "0.1.27" ]]; then
      echo "No trusted SHA-256 is available for clawpatch-supervise $version." >&2
      exit 2
    fi
    expected_sha256="$release_sha256_0_1_27"
  fi

  download_root="$(mktemp -d)"
  package_to_install="$download_root/clawpatch_supervise-${version}-py3-none-any.whl"
  "$python_command" - "$source_package" "$package_to_install" "$expected_sha256" <<'PY'
import hashlib
import hmac
import shutil
import sys
import urllib.request
from pathlib import Path

source, destination, expected = sys.argv[1:]
if len(expected) != 64 or any(character not in "0123456789abcdefABCDEF" for character in expected):
    print("CLAWPATCH_SUPERVISE_SHA256 must be a 64-character hexadecimal digest.", file=sys.stderr)
    raise SystemExit(2)

source_path = Path(source)
if source_path.is_file():
    shutil.copyfile(source_path, destination)
else:
    with urllib.request.urlopen(source) as response, open(destination, "wb") as output:
        shutil.copyfileobj(response, output)

with open(destination, "rb") as artifact:
    actual = hashlib.file_digest(artifact, "sha256").hexdigest()
if not hmac.compare_digest(actual, expected.lower()):
    print(f"Artifact SHA-256 mismatch: expected {expected.lower()}, found {actual}.", file=sys.stderr)
    raise SystemExit(2)
PY
fi

mkdir -p "$install_root"
staging_venv="$(mktemp -d "$install_root/venv.${version}.XXXXXX")"
"$python_command" -m venv "$staging_venv"
"$staging_venv/bin/python" -m pip install --disable-pip-version-check --upgrade "$package_to_install"

"$staging_venv/bin/clawpatch-supervise" --version
if [[ -n "$verify_repo" ]]; then
  PATH="${clawpatch_command%/*}:$PATH" \
    "$staging_venv/bin/clawpatch-supervise" doctor --repo "$verify_repo"
fi
printf '%s\n' "$clawpatch_installed_version"

mkdir -p "$bin_dir"
pending_supervisor_link="$(mktemp "$bin_dir/.clawpatch-supervise.XXXXXX")"
clawpatch_dir="${clawpatch_command%/*}"
"$python_command" - "$pending_supervisor_link" "$staging_venv/bin/clawpatch-supervise" "$bin_dir" "$clawpatch_dir" <<'PY'
import os
import shlex
import sys
from pathlib import Path

destination, supervisor, bin_dir, clawpatch_dir = sys.argv[1:]
content = "\n".join(
    (
        "#!/bin/sh",
        "export PYTHONUTF8=1",
        "export PYTHONIOENCODING=utf-8",
        "export NODE_DISABLE_COMPILE_CACHE=1",
        f"export PATH={shlex.quote(clawpatch_dir)}:{shlex.quote(bin_dir)}:\"$PATH\"",
        f"exec {shlex.quote(supervisor)} \"$@\"",
        "",
    )
)
Path(destination).write_text(content, encoding="utf-8")
os.chmod(destination, 0o755)
PY
# Stop EXIT cleanup from removing either valid target once activation begins.
activated_venv="$staging_venv"
staging_venv=""
mv -f "$pending_supervisor_link" "$bin_dir/clawpatch-supervise" || {
  move_status=$?
  staging_venv="$activated_venv"
  exit "$move_status"
}
pending_supervisor_link=""
if [[ "$clawpatch_command" == "$install_root/clawpatch/"* ]]; then
  ln -sfn "$clawpatch_command" "$bin_dir/clawpatch"
  clawpatch_command="$bin_dir/clawpatch"
fi
echo "Installed command: $bin_dir/clawpatch-supervise"

#!/usr/bin/env bash
set -euo pipefail

version="${CLAWPATCH_SUPERVISE_VERSION:-0.1.23}"
source_package="${CLAWPATCH_SUPERVISE_SOURCE:-https://github.com/uncmatteth/clawpatch-supervise/releases/download/v${version}/clawpatch_supervise-${version}-py3-none-any.whl}"
install_root="${CLAWPATCH_SUPERVISE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/clawpatch-supervise}"
bin_dir="${CLAWPATCH_SUPERVISE_BIN_DIR:-$HOME/.local/bin}"
python_command="${CLAWPATCH_SUPERVISE_PYTHON:-python3}"
readonly clawpatch_version="0.7.2"
readonly clawhub_version="0.19.1"
readonly release_sha256_0_1_23="660b53dea7ca1ea05520c602f094024d5f92819d17f191173b470b6e4d665960"
download_root=""
staging_venv=""
pending_supervisor_link=""

cleanup() {
  [[ -z "$pending_supervisor_link" ]] || rm -f "$pending_supervisor_link"
  [[ -z "$staging_venv" ]] || rm -rf "$staging_venv"
  [[ -z "$download_root" ]] || rm -rf "$download_root"
}
trap cleanup EXIT

check_command_version() {
  local command_name="$1"
  local expected_version="$2"
  shift 2
  if ! checked_version="$("$@")"; then
    echo "The $command_name command failed its version check." >&2
    exit 2
  fi
  if [[ "$checked_version" != "$expected_version" ]]; then
    echo "$command_name $expected_version is required; found ${checked_version:-unknown}." >&2
    exit 2
  fi
}

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
  check_command_version "ClawHub" "$clawhub_version" "$clawhub_command" --cli-version
  clawhub_installed_version="$checked_version"
else
  command -v npm >/dev/null 2>&1 || {
    echo "ClawHub is missing and npm is unavailable. Install Node.js 22 or newer, then rerun this installer." >&2
    exit 2
  }
  clawhub_command=""
  clawhub_installed_version=""
fi

if command -v clawpatch >/dev/null 2>&1; then
  clawpatch_command="$(command -v clawpatch)"
else
  command -v npm >/dev/null 2>&1 || {
    echo "npm is required to install ClawPatch." >&2
    exit 2
  }
  clawpatch_root="$install_root/clawpatch"
  npm install --prefix "$clawpatch_root" --no-fund --no-audit "clawpatch@${clawpatch_version}"
  clawpatch_command="$clawpatch_root/node_modules/.bin/clawpatch"
  test -x "$clawpatch_command" || {
    echo "ClawPatch installation did not create its command." >&2
    exit 2
  }
fi
check_command_version "ClawPatch" "$clawpatch_version" "$clawpatch_command" --version
clawpatch_installed_version="$checked_version"

package_to_install="$source_package"
if [[ ! -d "$source_package" ]]; then
  expected_sha256="${CLAWPATCH_SUPERVISE_SHA256:-}"
  if [[ -z "$expected_sha256" ]]; then
    if [[ -n "${CLAWPATCH_SUPERVISE_SOURCE:-}" ]]; then
      echo "CLAWPATCH_SUPERVISE_SHA256 is required for a custom wheel source." >&2
      exit 2
    fi
    if [[ "$version" != "0.1.23" ]]; then
      echo "No trusted SHA-256 is available for clawpatch-supervise $version." >&2
      exit 2
    fi
    expected_sha256="$release_sha256_0_1_23"
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

if [[ -z "$clawhub_command" ]]; then
  clawhub_root="$install_root/clawhub"
  echo "ClawHub is missing; installing clawhub@${clawhub_version} into $clawhub_root"
  npm install --prefix "$clawhub_root" --no-fund --no-audit "clawhub@${clawhub_version}"
  clawhub_command="$clawhub_root/node_modules/.bin/clawhub"
  test -x "$clawhub_command" || {
    echo "ClawHub installation did not create its command." >&2
    exit 2
  }
  check_command_version "ClawHub" "$clawhub_version" "$clawhub_command" --cli-version
  clawhub_installed_version="$checked_version"
fi

"$staging_venv/bin/clawpatch-supervise" --version
printf '%s\n' "$clawhub_installed_version"
printf '%s\n' "$clawpatch_installed_version"

mkdir -p "$bin_dir"
pending_supervisor_link="$(mktemp "$bin_dir/.clawpatch-supervise.XXXXXX")"
rm -f "$pending_supervisor_link"
ln -s "$staging_venv/bin/clawpatch-supervise" "$pending_supervisor_link"
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
if [[ "$clawhub_command" == "$install_root/clawhub/"* ]]; then
  ln -sfn "$clawhub_command" "$bin_dir/clawhub"
  clawhub_command="$bin_dir/clawhub"
fi

echo "Installed command: $bin_dir/clawpatch-supervise"
echo "ClawHub command: $clawhub_command"

#!/usr/bin/env bash
set -euo pipefail

version="${CLAWPATCH_SUPERVISE_VERSION:-0.1.30}"
source_package="${CLAWPATCH_SUPERVISE_SOURCE:-https://github.com/uncmatteth/clawpatch-supervise/releases/download/v${version}/clawpatch_supervise-${version}-py3-none-any.whl}"
install_root="${CLAWPATCH_SUPERVISE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/clawpatch-supervise}"
bin_dir="${CLAWPATCH_SUPERVISE_BIN_DIR:-$HOME/.local/bin}"
python_command="${CLAWPATCH_SUPERVISE_PYTHON:-python3}"
verify_repo="${CLAWPATCH_SUPERVISE_VERIFY_REPO:-}"
readonly minimum_clawpatch_version="0.7.2"
readonly release_clawpatch_version="0.7.2"
readonly release_clawpatch_integrity_0_7_2="sha512-rhpWj6e31XJUtWKlp/MJOjdjtj+ZXc9WiLcXRk+ZaA699K++dVaYfx00dVS/QNiJBaI71IUFU6sdSPsX/nyW0g=="
readonly release_sha256_0_1_30="0d8ebfbaa3e526afd458d572207af1f765e528c5e03daf20ea022a4ccc93854b"
download_root=""
staging_venv=""
staging_clawpatch_root=""
clawpatch_download_root=""
pending_supervisor_link=""
pending_clawpatch_link=""
previous_supervisor_command=""
previous_clawpatch_command=""
activation_started=false
activation_complete=false
install_lock_held=false
supervisor_destination=""
clawpatch_destination=""

cleanup() {
  local exit_status=$?
  local rollback_failed=false
  local retain_previous_supervisor_command=false
  local retain_previous_clawpatch_command=false
  trap - EXIT
  set +e
  if [[ "$activation_started" == true && "$activation_complete" != true ]]; then
    if [[ -n "$previous_supervisor_command" ]]; then
      if [[ -e "$previous_supervisor_command" || -L "$previous_supervisor_command" ]]; then
        if ! mv -f "$previous_supervisor_command" "$supervisor_destination"; then
          echo "Rollback failed for $supervisor_destination; previous command retained at $previous_supervisor_command" >&2
          rollback_failed=true
          retain_previous_supervisor_command=true
        fi
      elif ! rm -f "$supervisor_destination"; then
        echo "Rollback failed to remove $supervisor_destination" >&2
        rollback_failed=true
      fi
    fi
    if [[ -n "$previous_clawpatch_command" ]]; then
      if [[ -e "$previous_clawpatch_command" || -L "$previous_clawpatch_command" ]]; then
        if ! mv -f "$previous_clawpatch_command" "$clawpatch_destination"; then
          echo "Rollback failed for $clawpatch_destination; previous command retained at $previous_clawpatch_command" >&2
          rollback_failed=true
          retain_previous_clawpatch_command=true
        fi
      elif ! rm -f "$clawpatch_destination"; then
        echo "Rollback failed to remove $clawpatch_destination" >&2
        rollback_failed=true
      fi
    fi
  fi
  [[ -z "$pending_supervisor_link" ]] || rm -f "$pending_supervisor_link"
  [[ -z "$pending_clawpatch_link" ]] || rm -f "$pending_clawpatch_link"
  if [[ "$retain_previous_supervisor_command" != true && -n "$previous_supervisor_command" ]]; then
    rm -f "$previous_supervisor_command"
  fi
  if [[ "$retain_previous_clawpatch_command" != true && -n "$previous_clawpatch_command" ]]; then
    rm -f "$previous_clawpatch_command"
  fi
  [[ -z "$staging_venv" ]] || rm -rf "$staging_venv"
  [[ -z "$staging_clawpatch_root" ]] || rm -rf "$staging_clawpatch_root"
  [[ -z "$clawpatch_download_root" ]] || rm -rf "$clawpatch_download_root"
  [[ -z "$download_root" ]] || rm -rf "$download_root"
  if [[ "$install_lock_held" == true ]]; then
    exec 9>&-
  fi
  if [[ "$rollback_failed" == true ]]; then
    exit_status=1
  fi
  exit "$exit_status"
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
install_root="$("$python_command" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$install_root")"
bin_dir="$("$python_command" -c 'import os, sys; print(os.path.abspath(sys.argv[1]))' "$bin_dir")"
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
    match = re.search(
        r"(?<![0-9A-Za-z.-])"
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(-(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*)"
        r"(?:\.(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*))*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        r"(?![0-9A-Za-z.-])",
        value,
    )
    if not match:
        raise SystemExit(1)
    return tuple(map(int, match.groups()[:3])), match.group(4) is not None

actual, actual_is_prerelease = version(sys.argv[1])
minimum, minimum_is_prerelease = version(sys.argv[2])
raise SystemExit(
    actual < minimum
    or (actual == minimum and actual_is_prerelease and not minimum_is_prerelease)
)
PY
}

resolve_executable() {
  local command_path
  command_path="$(type -P -- "$1" 2>/dev/null)" || return 1
  "$python_command" - "$command_path" <<'PY'
import os
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
if not path.is_file() or not os.access(path, os.X_OK):
    raise SystemExit(1)
print(path)
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

managed_clawpatch=false
if clawpatch_command="$(resolve_executable clawpatch)" && \
  compatible_clawpatch "$clawpatch_command"; then
  :
else
  command -v npm >/dev/null 2>&1 || {
    echo "npm is required to install ClawPatch." >&2
    exit 2
  }
  mkdir -p "$install_root"
  staging_clawpatch_root="$(mktemp -d "$install_root/clawpatch.XXXXXX")"
  clawpatch_download_root="$(mktemp -d)"
  clawpatch_package="$clawpatch_download_root/clawpatch-${release_clawpatch_version}.tgz"
  npm pack --ignore-scripts --pack-destination "$clawpatch_download_root" \
    "clawpatch@$release_clawpatch_version" >/dev/null
  test -f "$clawpatch_package" || {
    echo "ClawPatch download did not create its release tarball." >&2
    exit 2
  }
  "$python_command" - "$clawpatch_package" "$release_clawpatch_integrity_0_7_2" <<'PY'
import base64
import binascii
import hashlib
import hmac
import sys

package, expected = sys.argv[1:]
algorithm, separator, encoded_digest = expected.partition("-")
try:
    expected_digest = base64.b64decode(encoded_digest, validate=True)
except (binascii.Error, ValueError):
    expected_digest = b""
if algorithm != "sha512" or not separator or len(expected_digest) != 64:
    print("The trusted ClawPatch SHA-512 pin is invalid.", file=sys.stderr)
    raise SystemExit(2)

with open(package, "rb") as artifact:
    actual_digest = hashlib.file_digest(artifact, "sha512").digest()
if not hmac.compare_digest(actual_digest, expected_digest):
    actual = "sha512-" + base64.b64encode(actual_digest).decode("ascii")
    print(f"ClawPatch artifact SHA-512 mismatch: expected {expected}, found {actual}.", file=sys.stderr)
    raise SystemExit(2)
PY
  npm install --prefix "$staging_clawpatch_root" --no-fund --no-audit \
    --ignore-scripts "$clawpatch_package"
  clawpatch_command="$staging_clawpatch_root/node_modules/.bin/clawpatch"
  test -x "$clawpatch_command" || {
    echo "ClawPatch installation did not create its command." >&2
    exit 2
  }
  managed_clawpatch=true
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
    if [[ "$version" != "0.1.30" ]]; then
      echo "No trusted SHA-256 is available for clawpatch-supervise $version." >&2
      exit 2
    fi
    expected_sha256="$release_sha256_0_1_30"
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

mkdir -p "$install_root" "$bin_dir"
exec 9>"$install_root/.install.lock"
"$python_command" - 9 <<'PY'
import fcntl
import sys

fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX)
PY
install_lock_held=true
supervisor_destination="$bin_dir/clawpatch-supervise"
clawpatch_destination="$bin_dir/clawpatch"
if [[ -d "$supervisor_destination" ]]; then
  echo "Command destination is a directory: $supervisor_destination" >&2
  exit 2
fi
if [[ "$managed_clawpatch" == true && -d "$clawpatch_destination" ]]; then
  echo "Command destination is a directory: $clawpatch_destination" >&2
  exit 2
fi
superseded_venv="$("$python_command" - "$supervisor_destination" "$install_root" <<'PY'
import shlex
import sys
from pathlib import Path

wrapper = Path(sys.argv[1])
install_root = Path(sys.argv[2]).resolve()
supervisor = None
if wrapper.is_symlink():
    supervisor = wrapper.resolve()
elif wrapper.is_file():
    try:
        lines = wrapper.read_text(encoding="utf-8").splitlines()
        for line in lines:
            words = shlex.split(line)
            if len(words) == 3 and words[0] == "exec" and words[2] == "$@":
                supervisor = Path(words[1]).resolve()
                break
    except (OSError, UnicodeError, ValueError):
        pass

if supervisor is not None and supervisor.name == "clawpatch-supervise":
    candidate = supervisor.parent.parent
    if (
        candidate.name.startswith("venv.")
        and candidate.parent == install_root
        and supervisor == candidate / "bin" / "clawpatch-supervise"
    ):
        print(candidate)
PY
)"
superseded_clawpatch_root=""
if [[ "$managed_clawpatch" == true ]]; then
  superseded_clawpatch_root="$("$python_command" - "$clawpatch_destination" "$install_root" <<'PY'
import os
import sys
from pathlib import Path

command = Path(sys.argv[1])
install_root = Path(sys.argv[2]).resolve()
if command.is_symlink():
    clawpatch = Path(os.readlink(command))
    if not clawpatch.is_absolute():
        clawpatch = command.parent / clawpatch
    clawpatch = Path(os.path.abspath(clawpatch))
    candidate = clawpatch.parent.parent.parent
    if (
        candidate.name.startswith("clawpatch.")
        and not candidate.is_symlink()
        and candidate.is_dir()
        and candidate.parent.resolve() == install_root
        and clawpatch == candidate / "node_modules" / ".bin" / "clawpatch"
    ):
        print(candidate)
PY
)"
fi
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
if [[ "$managed_clawpatch" == true ]]; then
  pending_clawpatch_link="$(mktemp "$bin_dir/.clawpatch.XXXXXX")"
  ln -sfn "$clawpatch_command" "$pending_clawpatch_link"
fi
activated_venv="$staging_venv"
previous_supervisor_command="$(mktemp "$bin_dir/.clawpatch-supervise.previous.XXXXXX")"
if [[ "$managed_clawpatch" == true ]]; then
  previous_clawpatch_command="$(mktemp "$bin_dir/.clawpatch.previous.XXXXXX")"
fi
"$python_command" - \
  "$supervisor_destination" "$previous_supervisor_command" \
  "$clawpatch_destination" "$previous_clawpatch_command" <<'PY'
import os
import shutil
import sys

for destination, backup in zip(sys.argv[1::2], sys.argv[2::2]):
    if not backup:
        continue
    os.unlink(backup)
    if os.path.islink(destination):
        os.symlink(os.readlink(destination), backup)
    elif os.path.exists(destination):
        shutil.copy2(destination, backup)
PY
activation_started=true
if [[ "$managed_clawpatch" == true ]]; then
  "$python_command" - "$pending_clawpatch_link" "$clawpatch_destination" <<'PY' || {
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
    move_status=$?
    exit "$move_status"
  }
  pending_clawpatch_link=""
  clawpatch_command="$clawpatch_destination"
fi
"$python_command" - "$pending_supervisor_link" "$supervisor_destination" <<'PY' || {
import os
import sys

os.replace(sys.argv[1], sys.argv[2])
PY
  move_status=$?
  exit "$move_status"
}
pending_supervisor_link=""
activation_complete=true
activated_clawpatch_root="$staging_clawpatch_root"
staging_venv=""
staging_clawpatch_root=""
echo "Installed command: $supervisor_destination"

#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

readonly TAG="${AI_WORKSTATION_TEST_TAG:?AI_WORKSTATION_TEST_TAG is required}"
readonly VERSION="${TAG#v}"
readonly PAGES_BASE="${AI_WORKSTATION_TEST_PAGES_BASE:-https://luigiverona.github.io/ai}"
readonly RELEASE_BASE="${AI_WORKSTATION_TEST_RELEASE_BASE:-https://github.com/luigiverona/ai/releases/download}"
readonly INSTALLER="/tmp/ai-bootstrap-test-install"
readonly TAMPERED_INSTALLER="/tmp/ai-bootstrap-test-install-tampered"
readonly ARCHIVE="/tmp/ai-bootstrap-test-${VERSION}.tar.gz"

[[ ${EUID} -eq 0 ]] || { printf 'bootstrap test must prepare its container as root\n' >&2; exit 1; }
[[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'invalid test tag\n' >&2; exit 1; }
curl -fsSL --proto '=https' "${PAGES_BASE}/install" -o "${INSTALLER}"
curl -fsSL --proto '=https' "${RELEASE_BASE}/${TAG}/ai-${VERSION}.tar.gz" -o "${ARCHIVE}"
expected_sha256="$(sed -n 's/^readonly EXPECTED_SHA256="\([0-9a-f]\{64\}\)"$/\1/p' "${INSTALLER}")"
[[ ${expected_sha256} =~ ^[0-9a-f]{64}$ ]] || { printf 'invalid embedded SHA-256\n' >&2; exit 1; }
printf '%s  %s\n' "${expected_sha256}" "${ARCHIVE}" | sha256sum -c -
python - "${INSTALLER}" "${VERSION}" "${expected_sha256}" <<'PY'
import pathlib
import sys

from tools.build_installer import validate_installer

validate_installer(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), sys.argv[2], sys.argv[3])
PY
bash -n "${INSTALLER}"
shellcheck "${INSTALLER}"

assert_common_install() {
  local user="$1"
  local home="/home/${user}"
  local release="${home}/.local/share/ai/releases/${VERSION}"

  [[ $(cat "${home}/.local/share/ai/.ai-workstation-installation") == "ai-workstation installation format 1" ]] || { printf 'installation marker is invalid for %s\n' "${user}" >&2; exit 1; }
  [[ -x ${home}/.local/bin/ai ]] || { printf 'launcher is missing for %s\n' "${user}" >&2; exit 1; }
  [[ $(sed -n '2p' "${home}/.local/bin/ai") == "# ai-workstation managed launcher format 1" ]] || { printf 'launcher marker is invalid for %s\n' "${user}" >&2; exit 1; }
  [[ -L ${home}/.local/share/ai/current ]] || { printf 'current link is missing for %s\n' "${user}" >&2; exit 1; }
  [[ $(readlink "${home}/.local/share/ai/current") == "releases/${VERSION}" ]] || { printf 'current link has the wrong target for %s\n' "${user}" >&2; exit 1; }
  [[ -f ${release}/pyproject.toml ]] || { printf 'installed project metadata is missing for %s\n' "${user}" >&2; exit 1; }
  [[ -f ${release}/src/ai_setup/cli.py ]] || { printf 'installed CLI is missing for %s\n' "${user}" >&2; exit 1; }
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/ai" --version
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/ai" --help
  runuser -u "${user}" -- env HOME="${home}" "${home}/.local/bin/ai" --dry-run
  [[ ! -e ${home}/.local/bin/codex-01 ]] || { printf 'Codex profile launcher exists before setup for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.local/bin/codex-02 ]] || { printf 'Codex profile launcher exists before setup for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.codex ]] || { printf 'default Codex state exists for %s\n' "${user}" >&2; exit 1; }
  [[ ! -e ${home}/.local/share/ai/codex ]] || { printf 'Codex profile state exists before setup for %s\n' "${user}" >&2; exit 1; }
  if find "${home}" -xdev -user root -print -quit | grep -q .; then
    printf 'root-owned file found in %s\n' "${home}" >&2
    exit 1
  fi
}

install_twice() {
  local user="$1"
  local home="/home/${user}"
  local output

  output="$(runuser -u "${user}" -- env HOME="${home}" \
    AI_WORKSTATION_RELEASE_BASE="${RELEASE_BASE}" \
    bash "${INSTALLER}")"
  grep -Fq 'The ai command is installed.' <<<"${output}"
  grep -Fq "Configuring the ${user#ai-} PATH... done." <<<"${output}"
  grep -Fq 'Run ai to set up the workstation.' <<<"${output}"
  if grep -Fq "${VERSION}" <<<"${output}"; then
    printf 'bootstrap output exposed the version banner\n' >&2
    exit 1
  fi
  if grep -Fq '% Total' <<<"${output}"; then
    printf 'bootstrap output exposed the curl transfer meter\n' >&2
    exit 1
  fi
  runuser -u "${user}" -- env HOME="${home}" \
    AI_WORKSTATION_RELEASE_BASE="${RELEASE_BASE}" \
    bash "${INSTALLER}" >/dev/null
  assert_common_install "${user}"
}

assert_unmodified_shell_file() {
  local path="$1"
  if [[ -f ${path} ]] && grep -Eq '(Added by ai|\.local/bin)' "${path}"; then
    printf 'unrelated shell file was modified: %s\n' "${path}" >&2
    exit 1
  fi
}

useradd --create-home --shell /usr/bin/fish ai-fish
useradd --create-home --shell /bin/bash ai-bash
useradd --create-home --shell /usr/bin/zsh ai-zsh
useradd --create-home --shell /bin/bash ai-bad-checksum

install_twice ai-fish
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'fish_add_path --global --move $HOME/.local/bin' /home/ai-fish/.config/fish/conf.d/ai.fish || true) -eq 1 ]] || { printf 'fish PATH entry is missing or duplicated\n' >&2; exit 1; }
assert_unmodified_shell_file /home/ai-fish/.bashrc
assert_unmodified_shell_file /home/ai-fish/.zshrc

install_twice ai-bash
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' /home/ai-bash/.bashrc || true) -eq 1 ]] || { printf 'Bash PATH entry is missing or duplicated\n' >&2; exit 1; }
assert_unmodified_shell_file /home/ai-bash/.config/fish/conf.d/ai.fish
assert_unmodified_shell_file /home/ai-bash/.zshrc

install_twice ai-zsh
# Compare the literal line written by the installer.
# shellcheck disable=SC2016
[[ $(grep -Fxc 'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac' /home/ai-zsh/.zshrc || true) -eq 1 ]] || { printf 'Zsh PATH entry is missing or duplicated\n' >&2; exit 1; }
assert_unmodified_shell_file /home/ai-zsh/.config/fish/conf.d/ai.fish
assert_unmodified_shell_file /home/ai-zsh/.bashrc

sed "s/${expected_sha256}/$(printf '0%.0s' {1..64})/" "${INSTALLER}" >"${TAMPERED_INSTALLER}"
if runuser -u ai-bad-checksum -- env HOME=/home/ai-bad-checksum \
  AI_WORKSTATION_RELEASE_BASE="${RELEASE_BASE}" bash "${TAMPERED_INSTALLER}"; then
  printf 'installer accepted an incorrect pinned checksum\n' >&2
  exit 1
fi
[[ ! -e /home/ai-bad-checksum/.local/bin/ai ]]
[[ ! -e /home/ai-bad-checksum/.local/share/ai/current ]]
[[ ! -e /home/ai-bad-checksum/.local/share/ai/releases/${VERSION} ]]

if compgen -G '/tmp/ai-bootstrap.*' >/dev/null; then
  printf 'temporary bootstrap paths remain\n' >&2
  exit 1
fi

printf 'isolated Arch bootstrap checks passed\n'

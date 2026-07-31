# ai

`ai` sets up and verifies an Arch Linux workstation through one guided CLI.

## Requirements

- Arch Linux on x86-64
- A normal, non-root user with sudo access
- Network access
- fish, Bash, or Zsh

## Installation

Install the current release:

```bash
curl -fsSL https://ai.luigiverona.dev/install | bash
```

To inspect the bootstrap before running it:

```bash
curl -fsSL https://ai.luigiverona.dev/install -o install
less install
bash install
```

The bootstrap downloads an immutable release archive from GitHub Releases and verifies
it against the checksum embedded in the installer. Piping a remote installer to a shell
does not independently verify the installer itself; see [Security](SECURITY.md) for the
trust boundaries.

Installation creates the `ai` launcher but does not run workstation setup. Run
`ai` afterward to inspect the plan and choose whether to continue.

## Usage

| Command | Purpose |
| --- | --- |
| `ai` | Set up the complete workstation. |
| `ai setup` | Set up the complete workstation explicitly. |
| `ai apps [CATEGORY ...]` | Install all applications or selected categories. |
| `ai git` | Configure Git identity. |
| `ai github` | Configure GitHub authentication and protocol. |
| `ai ssh` | Configure dedicated GitHub SSH access. |
| `ai codex` | Configure both managed Codex profiles. |
| `ai status` | Check workstation readiness without changing it. |

Common options:

- `--dry-run` shows what would happen without making changes.
- `--yes` accepts safe default confirmations, but never destructive SSH cleanup.
- `--verbose` shows detailed operations with configured secret redaction.

Application categories and their current contents come from the manifests under
[`apps/`](apps/). Run `ai apps --help` to see the available categories.

## What it configures

- a supported full Arch Linux system update;
- selected Arch, AUR, and Flatpak applications;
- per-user Flatpak and the Flathub remote;
- Git identity and default branch;
- GitHub authentication and SSH protocol;
- a dedicated GitHub SSH key and managed host configuration;
- isolated `codex-01` and `codex-02` profiles; and
- `~/.local/bin` on the active supported shell's PATH.

## Safety

`ai` refuses root execution and asks for confirmation before mutation. It performs a
full Arch upgrade instead of a partial upgrade, builds AUR packages without root, and
uses Flatpak's per-user scope. Dry runs do not execute mutation commands, while
`ai status` uses a read-only command runner.

Existing SSH keys are preserved by default. Eligible destructive cleanup requires a
separate confirmation that `--yes` cannot approve. Managed files use guarded,
atomic replacement, and unrelated files remain outside the managed surface. Codex
profiles have separate state and credentials.

Published releases contain an immutable, checksummed runtime archive. The installer
refuses unrelated install roots, launchers, and dedicated shell files; verifies an
extracted release before atomically activating it; and rolls back a failed activation.
The installer endpoint and upstream package sources remain trust boundaries. See
[SECURITY.md](SECURITY.md) for the complete current security model.

## Development

Create a virtual environment and install the project with its development tools:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e . ruff mypy
```

Run the principal checks:

```bash
python -m compileall src tests tools
python -m unittest discover
ruff check .
ruff format --check .
mypy src
mypy tools
bash -n bootstrap/install.in
python tools/build_release.py --tag v1.0.1 --check-only
git diff --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for change discipline and
[docs/design.md](docs/design.md) for the maintainer architecture.

## License

Licensed under the [MIT License](LICENSE).

# ai

`ai` is a small, explicit Arch Linux x86-64 workstation reconciler. It
checks each owned piece of state, repairs only drift, verifies changes, and
removes its component-local disposable files.

## Install

The release installer is intended to be served at:

```sh
curl -fsSL https://ai.luigiverona.dev/install | bash
```

Installation only installs `ai`; it never starts workstation reconciliation.
Release archives contain an executable `bin/ai` and are accompanied by a
`ai-VERSION.tar.gz.sha256` file.

To produce those two release inputs without retaining staging data:

```sh
tools/build-release /path/to/output
```

## Commands

```text
ai                 complete lifecycle
ai git             Git configuration only
ai github          GitHub authentication/protocol only
ai ssh             dedicated GitHub SSH identity only
ai --dry-run       inspect without persistent mutation
ai --verbose       print commands and diagnostics
```

The full lifecycle is precheck, software, Git, GitHub, SSH, Codex, and fish.
Git preserves an existing user identity, fills only missing identity values,
and manages `init.defaultBranch`. SSH state is isolated under the documented
`ai` paths. Flatpaks are per-user. AUR builds and Codex installation staging use
independent temporary directories removed on success or failure.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/pytest
```

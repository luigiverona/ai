# Contributing

## Scope

Changes should preserve the project's:

- Arch Linux and x86-64 focus;
- CLI-first, CLI-only supported interface;
- deterministic planning and output;
- explicit privilege, authentication, and filesystem boundaries; and
- separation between planning, mutation orchestration, and readiness verification.

Internal Python modules are implementation details, not a compatibility API.

## Development setup

Use Python 3.11 or newer:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e . ruff mypy
```

Do not install project development dependencies into the workstation's system Python.

## Validation

Run the complete local validation set:

```bash
python -m compileall src tests tools
PYTHONPATH=src python -m unittest discover
ruff check .
ruff format --check .
mypy src
mypy tools
bash -n bootstrap/install.in
shellcheck bootstrap/install.in
actionlint
python tools/build_release.py --tag v1.0.2 --check-only
git diff --check
```

ShellCheck and actionlint are required for complete validation even when one is not
available in a particular local environment. Report every unavailable check.

## Change discipline

- Inspect definitions, consumers, tests, and security boundaries before editing.
- Keep diffs scoped and add tests for behavioral changes.
- Avoid unrelated formatting and generated artifacts.
- Do not add hidden compatibility layers or aliases.
- Do not introduce broad abstractions without a concrete consumer.
- Do not create a supported public Python API.
- Use immutable full commit references for every third-party GitHub Action.
- Never commit secrets, credentials, private logs, release output, or temporary files.

Preserve user-owned data and existing workstation behavior unless a change explicitly
defines and tests a new contract.

User-visible output changes require deterministic transcript coverage. Test headings,
major-section spacing, prompts, focused recovery commands, partial failures, expected
interruptions, TTY and redirected output, and verbose redaction. Keep assertions focused
on intentional interaction contracts rather than incidental wrapping.

## Commit and pull request expectations

Use the repository's short imperative commit style, such as:

```text
docs: clarify installer trust boundary
fix: preserve provider-aware package readiness
refactor: centralize runtime definitions
```

A pull request should state:

- the behavior changed and why;
- security or trust-boundary impact;
- validation executed and its results;
- checks that were unavailable; and
- migration implications, including whether existing installations are affected.

Keep mechanical renames separate from behavioral refactors when practical.

## Release-sensitive areas

The following require focused tests and explicit review:

- `bootstrap/install.in` and installer rendering;
- release construction, validation, checksums, and asset selection;
- application and dependency manifests;
- SSH key eligibility and deletion;
- GitHub and Codex authentication;
- the pinned Codex installer digest;
- the Codex installer's complete upstream diff, served-byte equality with the recorded
  official OpenAI commit, redirect hosts, size and media bounds, exact-byte failure
  fixtures, and the non-executing maintainer audit tool whenever its trust pin changes;
- AUR clone, build, provider, and privilege policy; and
- GitHub Actions workflows and permissions.

Do not change a release-sensitive contract based only on local apparent non-use.

## Updating supply-chain pins

To update the `yay-bin` bootstrap, fetch the candidate commit directly from
`https://aur.archlinux.org/yay-bin.git`. Review its complete tracked tree, `.SRCINFO`,
`PKGBUILD`, declared source URLs and checksums, package names, architectures, hooks, file
operations, network commands, and privilege use. Reproduce the documented deterministic
tree digest, then update the single production pin and explicit contract tests together.
Record the commit, tree, digest, and reviewed content in the change.

To update an external GitHub Action, resolve the intended upstream release in the
action's official repository, review its release notes and diff, and replace the
workflow reference with the exact full commit SHA. Keep a nearby version comment for
reviewability. Run the workflow contract test to prove that no mutable external
reference remains.

Installer transaction changes require focused handled-failure and abrupt-termination
tests. Exercise fresh and replacement installs at every journal phase, use the
synchronized crash hooks rather than timing sleeps, rerun to prove reconciliation, and
verify journal, backup, staging, launcher, shell, and `current` state. Review every
rename, removal, and link replacement for exact-path validation plus file and parent
directory fsync. Never weaken malformed-journal refusal or delete unrecorded remnants.

Python managed-file changes require focused tests for trusted-root containment, every
ancestor type and owner, dedicated-file ownership recognition, same-directory exclusive
temporary creation, file and parent-directory fsync, idempotence, and failures before
and after atomic replacement. Keep third-party command output and temporary diagnostics
outside the persistent managed-file abstraction unless their ownership model changes
explicitly.

Package or readiness inspection changes require the full public dry-run and status
matrix against empty and representative disposable homes. Snapshot all persistent
state before and after, exercise state-initializing fake providers, and require exact
filesystem equality while retaining accurate existing-state detection.

SSH inventory or deletion changes require disposable-home tests for root, private, and
public symlinks; special files; ownership and hard links; strict public-key parsing;
protected path and inode aliases; exact GitHub fingerprint correlation; and complete
pre-deletion batch revalidation. Race fixtures must replace entries at explicit
boundaries, prove zero deletion on validation failure, and verify that no private-key or
remote-key material reaches output or logs.

# Design

## Scope

`ai` is a personal setup tool for fresh Arch Linux x86-64 workstations. Other
distributions and customized installation environments are unsupported.

It plans, applies, and verifies the maintainer's workstation setup. It
handles declared software, system updates, Git and GitHub access, a dedicated GitHub SSH
key, two isolated Codex profiles, Flatpak/Flathub, and shell PATH configuration.

It is not a general configuration-management framework, package-manager replacement, or
supported Python library. It does not model arbitrary distributions, package providers,
profile counts, or user-defined workflow plugins.

## Public interface

The CLI is the only supported public interface:

- `ai` and `ai setup` perform complete setup;
- `ai apps [CATEGORY ...]` installs all or selected application categories;
- `ai git`, `ai github`, `ai ssh`, and `ai codex` run focused setup;
- `ai status` performs complete read-only readiness verification.

`python -m ai_setup` and the `ai = ai_setup.cli:main` console entry point reach the same CLI.
Internal Python modules, classes, functions, and data models are not a compatibility API.

## Catalog

Applications and dependencies are declared in strict TOML manifests beneath `apps/` and
`deps/`. Each package records its source, identifier, display name, category, and kind.
Unknown keys, invalid values, and inconsistent manifest placement fail loading.

Loading is deterministic. Application categories are a public CLI selection dimension;
dependency categories remain catalog organization and metadata, not a selectable API.
The planner derives dependency packages from actual commands and selected application
sources rather than exposing a generic dependency query interface.

## Planning

A CLI invocation produces a selection containing requested capabilities, application
categories, and whether setup is complete. The planner computes prerequisite closure in
a fixed order, selects packages deterministically, and deduplicates them by source and
identifier.

Complete setup includes the complete workstation contract. Focused commands include only
their selected work and required prerequisites. AUR application selection adds the
bootstrap requirements needed for the supported helper. Flatpak application selection
adds Flatpak and Flathub requirements.

The plan contains only state consumed by runtime orchestration: selected capabilities,
prerequisites, and packages. Status is not a planning capability and never constructs a
selection or plan.

## Workflow

Before mutation, the setup workflow validates the host and inspects current package
state. It renders the deterministic plan, asks for confirmation, and then executes only
the selected stages.

One authoritative ordered stage specification drives stage selection, visible labels,
interruption text, and resume reporting. Handler dispatch remains explicit in the
workflow rather than becoming a generic plugin engine.

After mutation, the workflow performs a fresh readiness run. It does not reuse
pre-mutation package state. Successful readiness is rendered through the same presenter
used by status. An interruption reports the current stage, completed work, and remaining
stages so rerunning can safely re-inspect current state.

User output uses plain section headings with one blank line between major sections.
Plans describe intended actions; stage headings identify current execution. Expected
failures report the failed stage, preserved earlier stages, stages that did not run, and
the narrowest safe rerun command. Expected cancellation returns 130 without a traceback
or a false completion message. Output remains line-oriented and deterministic in TTY and
redirected environments, without cursor control, progress glyphs, or color-only meaning.

## Readiness

`ReadinessVerifier` is the single readiness orchestrator for setup and status. Its scope
contains capabilities and packages, allowing focused setup to verify only its contract
while status verifies the complete workstation.

Each readiness run uses one package `StateInspector` across all package checks. Separate
runs create fresh inspectors, preserving deliberate before/after probes. Low-level
checks cover the supported system, package providers, Flathub, Git identity, GitHub
authentication and protocol, SSH connectivity, both Codex profiles and their isolation,
and shell PATH configuration.

Status uses `ReadOnlyRunner` as a defense-in-depth guard against mutation commands. It
does not construct the mutation workflow, request sudo, create a workspace, prompt for
confirmation, or call the planner. Ready status exits 0, incomplete status exits 1,
interruption exits 130, and parser errors exit 2.

## Execution

Commands carry explicit argument vectors and a mutation flag. Normal execution uses
subprocesses without shell command construction. Dry-run skips mutation commands while
retaining plan and state reporting.

Interactive authentication is attached to the terminal and receives a scoped
environment. Replacement environments are used where isolation is required. Verbose
rendering redacts configured secrets. Command failures produce concise errors and may
store complete output in private workspace logs.

Temporary workspaces use a race-safe project prefix, restrictive permissions, and
purpose-specific child directories. Successful workspaces are removed unless explicitly
preserved; exceptions preserve them for diagnosis. Cleanup requires both containment
within the temporary root and the expected project prefix.

## Packages

Pacman operations use a supported full `-Syu` upgrade rather than partial upgrades.
Native package state is checked through the package database.

The AUR path accepts a runnable installed provider satisfying `yay`, including `yay` or
`yay-bin`. When none is ready, the bootstrap fetches one reviewed full commit directly
from the official `yay-bin` repository and checks it out detached. Before `makepkg`
runs, validation requires the exact commit and origin, one origin remote, no submodules,
a clean set of regular tracked files, the pinned tree digest, and the expected exact
package metadata.

The tree digest hashes bytewise-sorted records containing each tracked relative path,
Git file mode, regular-file type, and SHA-256 content digest. It excludes timestamps,
absolute paths, and `.git` storage; untracked or ignored entries and unsupported types
fail validation. `makepkg` runs as the normal user. Sudo is limited to installation of
the selected built package. Output selection prevents unrelated split/debug artifacts
from becoming top-level installations.

Flatpak configuration and application installation use per-user scope. One shared
Flathub readiness probe determines whether the user remote exists; setup adds it only
when missing, and post-mutation readiness probes again.

Package verification remains source-aware for pacman/AUR providers, Flatpak
applications, and the managed upstream Codex executable.

Inspection subprocesses have a narrower environment boundary than mutation commands.
Absent Flatpak user state is detected before Flatpak is invoked. Existing Flatpak data
remains visible while cache, configuration, state, runtime, and temporary paths use a
disposable root outside the target home. The installed AUR helper must satisfy package
dependency and executable-ownership checks; its runtime check uses the same disposable
probe environment. Probe roots are removed when inspection finishes and are never used
for mutating setup commands.

## Configuration

### Git

Git configuration owns the selected identity values and `init.defaultBranch`. It reads
existing values before writing and leaves unrelated global configuration unchanged.

### GitHub

Shared probes own the exact authentication-status and Git-protocol checks. Setup performs
interactive login only when required, verifies authentication afterward, configures SSH
protocol when needed, and verifies again. GitHub login does not manage SSH keys.

### SSH

SSH configuration owns one dedicated GitHub private/public key pair and one managed host
fragment. It inspects only those dedicated paths. An absent pair is created; an exact,
user-owned, single-link regular Ed25519 pair is reused; an incomplete, malformed,
mismatched, symbolic, hard-linked, or wrong-owner collision is refused unchanged.
Unrelated local and GitHub keys are neither inventoried nor modified. Connectivity is
checked with only the dedicated identity before registration is attempted.

### Codex

One ordered immutable profile specification defines `codex-01` and `codex-02`. Both use
the shared managed binary but set distinct `CODEX_HOME` directories. Installation,
authentication, readiness, and isolation verification consume that same specification.
An unrelated generic `~/.local/bin/codex` path is outside the managed surface.

The official installer is a fail-closed external-code boundary. One provenance record
defines its canonical HTTPS endpoint, permitted redirect hosts, maximum byte count,
accepted shell media, exact official OpenAI source commit and path, audit date, and
SHA-256. Downloaded bytes are never normalized and are executed only after every bound
and the exact digest pass. A maintainer audit compares the served file with the pinned
official source without executing it or changing the trusted digest automatically.

### Shell

Shell detection targets an interactive fish, Bash, or Zsh session, with the account's
login shell as a fallback. Configuration updates one appropriate startup path, preserves
unrelated content, rejects unsafe symlinks, and reports whether a new session is needed.

### Managed files

One internal writer owns complete replacement of Python-managed configuration files and
launchers. Callers supply an absolute trusted root, target, expected owner, exact mode,
desired bytes, and an existing-file contract. The writer validates the root and walks
each existing ancestor using non-following directory descriptors. Missing managed
directories are created one component at a time and their parent directories are
fsynced.

Replacement uses an exclusive, restrictive temporary file in the target directory.
After writing, chmod, and file fsync, the writer revalidates the target, atomically
replaces it, fsyncs the parent directory, and verifies the final bytes, type, owner, and
mode. Correct files are not rewritten. Bash and Zsh startup files use a user-owned-file
contract so unrelated bytes survive managed-block updates; dedicated SSH, fish, and
Codex launcher files require their precise managed content.

`ssh-keygen`, Git, GitHub CLI, Codex authentication, and package managers continue to
own their respective output formats. Temporary AUR configuration and diagnostic logs
remain scoped to the private workspace rather than being treated as persistent managed
configuration. An unrecognized dedicated path is never adopted or replaced. The error
names the exact path, confirms that it was left unchanged, and directs the user to
inspect and resolve the collision before rerunning the focused stage. File contents are
not printed. Atomic replacement prevents partial-file visibility, but concurrent
valid writers otherwise have last-completed-write semantics.

## Identity and paths

One immutable runtime identity definition owns the command, display, distribution,
repository, endpoint, filenames, markers, and relative managed paths used by Python.
Absolute paths derive from an injected home at call time; the package does not capture
`Path.home()` during import.

`pyproject.toml` is the sole authored version source. Source and extracted-release
execution read the trusted adjacent project metadata; installed execution can fall back
to distribution metadata. Wrong project names and malformed or unrelated metadata are
rejected.

Release tools use a narrow pure-data identity boundary with explicit contract tests
against runtime identity. The standalone Bash installer retains rendered literals
because it must operate before the Python package is installed.

## Release

Every release has exactly three assets:

- `install`
- `ai-<version>.tar.gz`
- `SHA256SUMS`

The builder selects tracked runtime files and normalizes archive metadata. Independent
validation checks paths, file types, permissions, checksums, installer rendering, and
runtime execution. The release workflow builds twice from clean checkouts and compares
the bytes before upload.

The bootstrap marks the installation root and wholly managed integration files. Each
run downloads, verifies, extracts, and validates the immutable archive before comparing
the complete existing release tree by type, mode, and content digest. A per-user lock
serializes mutation and startup recovery.

A strict JSON journal beneath the managed root records one transaction identifier,
phase, prior-state flags and digests, the prior `current` target, and exact
installer-owned staging and backup paths. The ordered phases cover preparation,
verified extraction, prior-state preservation, release installation, launcher and shell
replacement, `current` switching, commit, and cleanup. The journal is never sourced or
evaluated; parsing rejects unknown keys, duplicate keys, unsafe paths, unsupported
versions, symlinks, wrong types, owners, or modes.

Before the durable commit point, handled rollback and next-run reconciliation restore
the prior release, launcher, shell file, and `current` link, or remove transaction-owned
fresh-install state. After the committed journal record, recovery validates and retains
the new installation and removes only recorded backups and staging. Missing or modified
recovery material fails closed. Objects that merely resemble remnants but are not named
by a valid journal are reported and preserved.

Critical files and complete release trees are fsynced before atomic replacement.
Affected parent directories are fsynced after marker and journal updates, release and
backup renames, launcher and shell replacement or restoration, `current` replacement,
and transaction cleanup. This establishes the intended ordering at the filesystem API
boundary without claiming guarantees beyond the filesystem and storage hardware.

GitHub Releases are the authority for immutable assets and attestations. The installer
embeds the archive checksum and downloads the exact tagged archive. Pages verifies the
published release and deploys only the identical `install` asset.

All external GitHub Actions are referenced by reviewed full commit SHAs. Version
comments preserve the upstream release context without making a mutable tag executable.

## Invariants

Tests enforce these critical invariants:

- the supported public interface is one CLI and one console entry point;
- public command plans, package sets, capabilities, and stages are deterministic;
- status remains planner-independent, workflow-independent, and read-only;
- setup and status share readiness orchestration and rendering;
- post-mutation verification uses fresh state;
- stage and Codex profile definitions each have one ordered source of truth;
- managed paths derive from identity and an injected home;
- unrelated SSH, shell, and Codex content is preserved;
- unrelated SSH keys remain outside the inspected and managed surface;
- authentication environments do not leak globally;
- version and release identity agree across runtime, tools, and installer;
- release construction is reproducible and limited to three intended assets.

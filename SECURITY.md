# Security

`ai` is a personal setup tool for fresh Arch Linux x86-64 workstations. Other
distributions and customized installation environments are unsupported.

## Supported versions

Only the latest published release receives security fixes.

## Reporting vulnerabilities

Do not disclose a vulnerability in a public issue. If GitHub private vulnerability
reporting is available for this repository, use it. Otherwise, contact the maintainer
through the repository owner's [GitHub profile](https://github.com/luigiverona) without
including vulnerability details in a public post, and request a private channel.

## Trust boundaries

The custom endpoint at <https://ai.luigiverona.dev/install> distributes the bootstrap
installer. The generated bootstrap embeds the expected SHA-256 for one immutable runtime
archive and downloads that archive from its exact GitHub Release tag.

The custom endpoint is not an independent trust root. If it were compromised, an
attacker could substitute both installer logic and its embedded checksum. GitHub Release
assets, checksum files, and build-provenance attestations provide a separate surface for
comparison and verification.

The following remain external trust boundaries:

- Arch Linux repositories and their signing infrastructure;
- the reviewed, commit-pinned `yay-bin` AUR recipe and its declared sources;
- Flathub and application publishers;
- GitHub authentication, Releases, and repository services; and
- OpenAI's Codex distribution infrastructure.

The official Codex installer script is accepted only when its exact bytes, HTTPS
redirect destination, bounded size, media type, interpreter, and SHA-256 match the
provenance reviewed in the source. The provenance records the canonical endpoint,
approved hosts, official repository path, exact upstream commit, audit date, and digest.
A changed upstream installer fails closed before execution. Maintainers compare served
bytes with that exact official source revision and review the complete upstream diff;
the audit tool reports evidence but never updates trust data automatically.

The `yay-bin` bootstrap fetches one exact commit from the official AUR Git repository.
Before `makepkg` runs, the checkout must match its expected origin, commit, tracked entry
types, complete deterministic tree digest, package base, and package names. This pins
the reviewed recipe, not the external binaries named by its checksummed source entries,
and does not make the AUR or those upstream sources risk-free.

Every external GitHub Action is referenced by a full commit SHA. Those commits remain
third-party code executed by GitHub-hosted runners; pinning prevents a tag from moving
without making the action intrinsically trusted.

## Command execution

Subprocesses use explicit argument vectors rather than shell command construction.
Commands are classified as read-only or mutating; dry-run mode skips mutating commands.
Interactive GitHub and Codex authentication uses scoped environments instead of changing
the process environment globally.

Dry-run and status inspection avoids state-initializing provider commands when their
persistent user state is absent. Provider execution that remains necessary uses
disposable XDG cache, configuration, state, runtime, and temporary roots outside the
target home. Existing Flatpak data remains visible for accurate application and remote
inspection.

Configured secret values are redacted from verbose command rendering. Failure output is
written only when needed, in private logs under the temporary workspace. Authentication
tokens and credential-file contents are not intentionally printed or logged.

## Privilege boundaries

The program refuses to run as root. Sudo is used for the system package operations that
require it and for validating cached credentials before privileged stages. AUR clones
and `makepkg` run as the normal user; elevation is limited to installing the selected
built package. Flatpak operations use per-user scope.

## Filesystem protections

Python-managed configuration and launcher updates use exclusive temporary files in the
target directory, fsync each completed file, replace atomically, and fsync the containing
directory. The writer requires an explicit trusted root, validates user ownership, and
opens each existing ancestor without following symlinks. Dedicated managed files are
replaced only when their exact ownership contract is recognized; user startup files
retain unrelated bytes.

Write or file-fsync failures leave the original target in place. A directory-fsync
failure after replacement is reported as an ambiguous durability failure because the new
file may already be visible; the program does not claim to restore an old file after
that point. SSH state, Codex profile state, temporary workspaces, and failure logs use
restrictive permissions appropriate to their contents.

Temporary workspace cleanup verifies containment and the project-owned prefix before
removal. Unrelated files are preserved by default and are not adopted merely because
their names resemble managed files.

When a dedicated managed path already contains unrecognized data, setup refuses the
replacement, leaves the path unchanged, and reports the collision for manual inspection.
It does not infer ownership from the filename or suggest an automatic destructive fix.

The bootstrap recognizes its installation root, launcher, and dedicated fish file only
through exact ownership markers on user-owned regular paths. It compares an existing
same-version tree with the checksum-verified extraction by type, mode, and file digest.
Activation is serialized per user and uses a strict, versioned, user-owned transaction
journal. Handled failures roll back immediately. After an abrupt termination, the next
installer run reconciles only the exact paths recorded by the journal before beginning
new work; unrecorded remnants are preserved and reported.

Files are flushed before critical atomic replacements, and affected directories are
fsynced after ownership-marker, journal, release, launcher, shell, `current`, backup,
restoration, and cleanup changes. The durable commit point is recorded only after all
active surfaces have been validated and flushed. These measures reduce crash and
power-loss ambiguity but remain subject to filesystem, kernel, storage-controller, and
hardware durability behavior.

## SSH key handling

`ai` owns only the dedicated private/public pair at
`~/.ssh/id_ed25519_ai_github{,.pub}` and `~/.ssh/config.d/ai-github.conf`, plus its exact
include line in `~/.ssh/config`. It does not inventory, open, correlate, list, or delete
unrelated local or GitHub SSH keys. Dedicated paths must be user-owned, single-link
regular files; symbolic, incomplete, malformed, mismatched, or otherwise unrecognized
pairs are refused and left unchanged. Private-key bytes are never printed or logged.

## Codex isolation

The supported launchers are `codex-01` and `codex-02`. Each sets a distinct
`CODEX_HOME` beneath `~/.local/share/ai/codex`, while both use the managed shared
binary beneath the installation root. Authentication and readiness are checked for each
profile independently.

Credential-file permissions are inspected, but credential contents are not read. An
unrelated generic `~/.local/bin/codex` file or symlink is not managed, rewritten,
deleted, or required to be absent.

## Release integrity

Release tooling selects tracked runtime inputs and normalizes archive order, ownership,
permissions, timestamps, and gzip metadata. Two clean independent builds must produce
identical bytes before publication.

Each release is limited to three intended assets:

- `install`
- `ai-<version>.tar.gz`
- `SHA256SUMS`

The archive and installer are independently validated before and after upload. The
release workflow creates build-provenance attestations and verifies the published
immutable release and its assets. GitHub Pages validates the release and deploys only
the exact `install` asset; it does not expose the archive or checksum files.

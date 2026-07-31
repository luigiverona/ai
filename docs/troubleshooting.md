# Troubleshooting

## Setup stopped partway through

Earlier completed stages remain in place. Read the final summary for the failed stage
and the narrowest safe rerun command. Resolve the reported cause, run that focused
command, and use `ai status` to check overall readiness. Add `--verbose` to the rerun for
safe command and error details.

## A managed path is not recognized

`ai` leaves an unrecognized file unchanged. Inspect the exact path named in the error
and decide how to preserve or relocate your file. After resolving the collision, rerun
the command shown in the final summary. `ai` does not adopt, rename, overwrite, or delete
the file automatically.

## The Codex installer differs from the audited version

No installer was executed. OpenAI changed the served installer bytes, so a reviewed
`ai` release must update the audited provenance before setup can continue. Do not bypass
the check or pipe the installer directly to a shell.

## Setup was interrupted

Expected interruption exits with status 130 and stops before any later stage begins.
Rerun the command shown in the interruption summary; current state is inspected again.

## Check readiness

Run `ai status`. It is read-only and noninteractive. A ready workstation exits zero;
an incomplete workstation exits one and reports the areas that still need attention.

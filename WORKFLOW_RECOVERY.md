# Workflow recovery and safety contract

## Package state

A package marker binds extraction to its archive SHA-256, selected masters, and
processing version. Completed extraction is reusable only for the same identity.
Replacements extract privately before publication. Package step records are
authoritative for modern stages; the older GUI readiness file is only a fallback
for unrecorded legacy folders. An unanswered option window is cancellation, not
acceptance. BSA checks record packed, explicitly skipped, or below-threshold
outcomes.

## Deployment transaction

`transactions/major-master-deploy/` contains flushed write-ahead intents and
snapshots of the deployment manifest, INI, catalog, and mutable journal ledgers.
Intent is recorded before a backup move or hardlink. Package-independent originals
live under `deployment_backups/`, not in disposable staging.

The manifest and journal must both succeed before the durable commit marker is
written. An uncommitted transaction is rolled back before a new deployment,
removal, or GUI recovery continues. A committed record requires cleanup only.
Unexpectedly changed files cause recovery to stop rather than overwrite them.
This is process-crash recovery, not a promise of atomicity across physical disk
failure, damaged filesystems, or broken storage hardware.

Do not delete transaction directories to dismiss an error. Preserve them with the
game and manager state for investigation. Recovery records are private local
state and must not be edited or replaced with records from another installation.

## Deletion and removal

Unstage checks the entire package against installed siblings, deployment
references, and fresh external plugin dependencies before deleting anything.
Staging reset and replacement share the backend guard. Removal uses recorded
installed hashes as well as hardlink identity; modified and legacy-unhashed
targets are left pending for review.

## External tools and concurrency

GUI jobs and backend mutations use non-blocking OS locks for the manager/game
roots. The job's own managed child processes inherit a scoped lock delegation
token, so a scanner can journal an override without conflicting with its parent;
unrelated instances cannot run the same mutation concurrently.
On POSIX, managed children inherit the held lock descriptors, keeping a new
instance out even after an unexpected parent exit. On Windows, managed tool
processes are assigned to a kill-on-close Job Object so parent exit ends the
owned process tree.

GUI tools and BSAPack share process ownership. Packing runs on the package volume,
checks free space conservatively, logs full output with a bounded in-memory tail,
checks cancellation, and durably tracks archive publication and individual loose
asset moves. Failure/restart restores unpublished work before a retry.
Generated archives record their exact hashes and retained input locations.
Reopening options restores those loose inputs and removes only unchanged,
MWManager-generated archives before rebuilding; modified archives stop for review.

Ghost-static override changes preserve originals in the install journal, use a
recoverable transaction, and set a persistent distant-land regeneration notice.
The user must explicitly acknowledge regeneration; MWManager does not claim to
run MGE XE's generator itself.

## Validation

Run `xvfb-run -a python -m pytest -q tests` on Linux, or
`python -m pytest -q tests` on Windows. The added workflow runs both platforms on
Python 3.12 and 3.14. Temporary-fixture tests cover process death, ledger failures,
package ownership, cancellation, cache invalidation, wrapped assets, and recovery.
Live Nexus challenges, antivirus locks, real BSAPack archives, MGE regeneration,
and Windows reparse/permission behavior still require supported-platform testing.

Local verification for this change: Python 3.14.8 on Linux/Xvfb, **327 passed,
7 skipped**. Undefined-name/syntax checks also pass for the changed standalone
modules. The skipped and mocked cases do not establish real Windows, real
BSAPack, live Nexus, or game-runtime compatibility; the Windows CI jobs provide
additional platform coverage, not a substitute for a real-install acceptance run.

# Threaded Queue for Major-Master Batch Staging

Status: proposal  
Date: 2026-11-20  
Audience: MWManager maintainers and implementation agents  
Scope: major-master archive staging, FOMOD/BAIN option collection, Tidy, BSA preparation, dependency discovery, and interruption recovery

## Executive summary

The current major-master workflow is serial:

1. extract one archive;
2. run Tidy and open its FOMOD options;
3. finish Tidy;
4. move to the next archive.

That structure minimizes concurrency but makes large batches feel much slower than necessary. Extraction and Tidy are the long non-interactive phases; FOMOD choice collection is the short interactive phase. They should be decoupled.

Recommended design:

- one **durable batch coordinator** owns the major-master staging workflow;
- one bounded **worker pool** performs archive extraction, pre-option normalization, Tidy, post-option apply, asset checks, BSA work, and catalog preparation;
- one **UI dispatcher on the Tk thread** presents option windows in a deterministic order;
- a per-package **state machine plus persisted queue** records what is ready, waiting for user input, running, complete, failed, or blocked;
- every package that can be extracted is prepared in the background before or while its FOMOD is being answered;
- every newly discovered FOMOD is raised to the UI immediately, even if it appears after extraction or from a dependency archive;
- once a package's FOMOD is complete, its Tidy/apply phase resumes immediately without waiting for unrelated option windows;
- dependent plugins discovered by selected options are appended to the same batch queue and participate in the same state machine.

The target user experience is:

> review the FOMOD choices for package A, then B, then C, with almost no file-work delay between dialogs, while unrelated extraction and Tidy continue in the background.

This is preferable to one thread per package. The queue is durable, bounded, cancellation-aware, and easier to test. Threading is an implementation detail of the queue; it is not the source of truth.

## Goals

1. Front-load as many FOMOD/BAIN decisions as practical.
2. Let extraction and non-interactive Tidy run in parallel with user decisions.
3. Keep all Tk creation, prompts, and widget updates on the main thread.
4. Preserve strict per-package ordering and exact `--apply` plan validation.
5. Avoid more than one writer mutating the same package.
6. Bound CPU, disk, process, memory, and file-descriptor pressure on large archives.
7. Continue safely after cancellation, GUI termination, child-process failure, or package failure.
8. Reopen at the exact package/phase that was incomplete.
9. Reoffer previously declined compatibility options when their target mod becomes available.
10. Extend naturally to required regular plugins found by major-master options.
11. Surface newly unpacked FOMODs as soon as they are discovered, without pausing unrelated background work.

## Non-goals

- Running multiple `--apply` passes against the same package concurrently.
- Opening several FOMOD selector windows simultaneously.
- Letting background workers mutate live `Data Files`.
- Deploying a package before all selected assets, compatibility choices, and plan checks are complete.
- Replacing durable transaction/journal state with in-memory thread state.
- Holding one OS process open for every archive.

## Current architecture

Relevant behavior is spread across:

- `mwmanager_gui.py`
  - `stage_major_masters`
  - `process_package`
  - `run_tidy_steps`
  - `waiting_option_archives`
  - `_resolve_waiting_options`
  - `_install_plugin_archives`
- `core_addons.py`
  - `stage_core_master_archives`
  - `package_steps`
  - `begin_package_step`
  - `complete_package_step`
  - `fail_package_step`
- `mock_fomod.py`
  - FOMOD/BAIN discovery
  - option selection
  - answer files
  - `mwmanager_pending_options.json`
  - compatibility reevaluation
- `managed_processes.py`
  - child ownership, polling, cancellation, and heartbeat support
- `workflow_safety.py`
  - durable transactions and mutation locks
- `major_master_deploy.py`
  - final deployment only

The existing package marker already contains a useful durable step record:

- extracted;
- tidied;
- BSA checked.

That record should be extended, not discarded.

## Recommended architecture

### 1. Batch coordinator

Add a dedicated module:

```text
mwmanager_batch.py
```

It owns:

- the durable batch manifest;
- package queue state;
- worker scheduling;
- priority calculation;
- dependency propagation;
- completion accounting;
- event delivery to the GUI;
- safe retry and cancellation.

It does not create Tk widgets and does not directly interpret arbitrary UI actions. It exposes:

```python
start(batch: BatchRequest) -> Batch
resume(batch_id: str) -> Batch
cancel(batch_id: str) -> None
poll(batch_id: str) -> list[BatchEvent]
submit_answers(package_id: str, answer_payload: dict) -> None
status(batch_id: str) -> BatchStatus
```

### 2. Per-package state machine

Do not use a single “staged/not staged” boolean.

Use these durable phases:

```text
DISCOVERED
QUEUED_FOR_EXTRACTION
EXTRACTING
EXTRACTED
QUEUED_FOR_OPTION_PREP
PREPARING_OPTIONS
AWAITING_OPTIONS
OPTIONS_ANSWERED
QUEUED_FOR_TIDY
TIDY_PLANNING
TIDY_APPLYING
TIDY_COMPLETE
DEPENDENCY_DISCOVERY
WAITING_ON_DEPENDENCIES
READY_FOR_DEPLOYMENT
BLOCKED
FAILED_RETRYABLE
CANCELLED
```

Recommended terminal states:

```text
READY_FOR_DEPLOYMENT
BLOCKED_USER_ACTION
FAILED_PERMANENT
CANCELLED
```

Every transition records:

- timestamp;
- previous phase;
- attempt count;
- PID/process ownership when applicable;
- worker slot;
- last error;
- package marker generation/hash;
- answer-file hash;
- source-archive path/hash;
- dependency keys.

### 3. Durable batch manifest

Add:

```text
outputs/mwmanager_batch_<id>.json
```

or one active manifest:

```text
outputs/mwmanager_active_batch.json
```

The first design is easier to retain/audit; the latter is simpler for one active GUI batch. Use one active manifest plus a completed-history folder if only one batch can run at a time.

Proposed shape:

```json
{
  "version": 1,
  "batch_id": "20261120T...",
  "kind": "major_masters",
  "phase": "collecting_options",
  "created_at": "...",
  "source": "downloads|resume|queue",
  "packages": {
    "package-key": {
      "display_name": "...",
      "archive": "...",
      "archive_sha256": "...",
      "staging_package": "...",
      "master_names": ["..."],
      "phase": "AWAITING_OPTIONS",
      "attempts": 2,
      "last_error": null,
      "blocked_by": [],
      "dependencies": [],
      "option_identity": "...",
      "answers_sha256": "...",
      "worker_slot": null
    }
  },
  "ready_order": ["package-key"],
  "dependency_queue": [],
  "cancel_requested": false
}
```

Write this file through one coordinator-owned lock:

1. serialize and validate the complete next manifest;
2. write a sibling `.tmp` file in the same directory;
3. flush/fsync the temporary file;
4. atomically replace the manifest;
5. fsync the directory where supported.

The manifest version drives explicit migrations. Keep one known-good `.bak` after successful replacement. No worker writes the manifest directly. It is the recovery truth; queue objects in RAM are disposable.

### 4. Worker pool

Use the Python standard library `concurrent.futures.ThreadPoolExecutor`.

Threads are appropriate here because the slow work is mostly:

- filesystem traversal;
- hashing large files;
- waiting on 7-Zip, LuaJIT, Texconv, FFmpeg, or BSA tooling;
- subprocess pipes;
- Tk remains the only UI thread.

Start with conservative limits:

| Queue | Default concurrency | Rationale |
| --- | ---: | --- |
| Extract/inspect | 1 | protects archive/AV/disk contention; resume is predictable |
| Tidy/asset work | 1 for same drive | Tidy is I/O-heavy and current mutation locks are intentionally strict |
| Independent post-option CPU analysis | 2 | only if packages are disjoint and the tools use separate scratch/output state |
| BSA packing | 1 | large files and external tool contention |
| Catalog/DB preparation | 1 | SQLite remains serialized |

The first version should use **one background staging worker plus asynchronous preparation tasks**, not full parallel file mutation. This still achieves the desired UX because extraction and option preparation can run ahead of the interactive UI.

Do not increase concurrency until real timings show a disk/CPU bottleneck that parallelism solves.

### 5. Tk interaction queue

Workers never open Tk windows.

They emit typed events:

```python
OptionsReady(package_id, option_records, preview_data)
FomodDiscovered(package_id, option_records, source, priority)
Progress(package_id, phase, current, total, text)
DependencyFound(package_id, need, candidates)
PackageReady(package_id)
PackageBlocked(package_id, reason)
PackageFailed(package_id, error)
BatchComplete(batch_id)
```

The coordinator converts every discovery into one canonical `OptionsReady` queue item after it has parsed and persisted the option catalog. `FomodDiscovered` is the worker/coordinator audit event; the Tk dispatcher consumes `OptionsReady`, never `FomodDiscovered` directly. A delivery record carries `discovery_id`, package identity/hash, priority, sequence, and `ready_at`.

The GUI polls a `queue.Queue` and:

1. opens one option dialog;
2. waits for that dialog's answers;
3. calls `submit_answers`;
4. moves immediately to the highest-priority queued `OptionsReady` item.

Persist the answer and the package's `OPTIONS_ANSWERED` phase together. For a newly published post-Tidy discovery,
the apply worker must wait even if the answer arrives before its waiter is registered; only an already-answered
discovery from an earlier pass may take the resume shortcut. Compare fingerprints after the waiter is released
before deciding whether Tidy must run again.

The option dialog is serialized because:

- it reduces cognitive overload;
- FOMOD decisions are stateful;
- the user cannot safely compare multiple nested selectors at once;
- package dependencies may change while an earlier dialog is open.

#### Newly discovered FOMOD priority

FOMOD discovery is an event, not a one-time scan result. A FOMOD can appear:

- after a wrapper archive has been extracted;
- inside a nested archive or package;
- after a companion/dependency download is staged;
- after Tidy moves an installer tree into `Extras`;
- after a selected package option activates another installer payload.

When a worker discovers a FOMOD, the coordinator:

1. persists a `FOMOD_DISCOVERED` event and the package's option catalog;
2. inserts it according to the priority rules in the interactive-choice queue;
3. leaves all unrelated extraction and Tidy workers running;
4. waits only for the currently open option dialog to save/cancel;
5. converts the parsed discovery to `OptionsReady` and opens it next when it has the highest eligible priority;
6. shows a short notice in batch status that a newly discovered installer was inserted next.

Recommended interactive priority:

1. dependency FOMODs required by an already answered option;
2. FOMODs discovered in the current package's selected payload;
3. original major-master FOMODs in configured load order;
4. companion/dependency FOMODs;
5. low-priority discovered installers from unrelated archives.

Do not preempt a live modal and discard unsaved state. “Raise immediately” means persist and announce immediately, then open next as soon as the current choice is complete.

The active selector owns its current form state in memory for the full modal lifetime. A discovery event only updates the batch header/queue. It does not rebuild the active form, invoke callbacks against the active dialog, or modify its pending choices. Closing the selector without Save leaves the package at `AWAITING_OPTIONS` and preserves the last saved answer, if any; crash recovery does not claim unsaved choices were accepted.

#### Keep work moving while FOMODs are open

Opening a FOMOD must not pause:

- extraction of queued archives;
- archive inspection and dependency lookup;
- documentation/image preparation;
- Tidy for packages whose options are already answered;
- BSA preparation for completed packages;
- dependency archive download-status polling.

Only mutation/final readiness for the same package waits. This is the key UX rule: the FOMOD queue can be active while useful background work continues.

### 6. Prefetch versus mutation

Separate “safe preparation” from “option-dependent mutation.”

#### Safe preparation

May run ahead:

- verify archive hash;
- copy archive into MWManager-owned archive storage;
- list archive members;
- inspect BSA contents;
- extract into a private `.extract-*` directory;
- discover FOMOD config and emit `FomodDiscovered`;
- build static option records;
- load local docs/images and Nexus notes;
- parse plugin masters;
- compute package size/file count.

#### Option-dependent mutation

Must wait for answers:

- activate selected FOMOD/BAIN payload;
- run post-option Tidy;
- rename/convert plugins;
- apply Lua and asset repairs;
- BSA pack;
- write final package marker complete state.

This separation is critical. It is what removes the long wait between option dialogs.

Activating a selected payload is itself a discovery phase. After activation, the worker must rescan that payload for nested FOMOD/BAIN installers and emit another persisted discovery event. This continues until no activated payload reveals a new installer. Limit nested activation to 16 levels and require a stable installer identity made from package identity, normalized installer path, config hash, and selected-payload fingerprint. Repeated identity is acknowledged idempotently and never queued twice. Each newly found installer joins the same priority queue; extraction and Tidy for other packages continue throughout.

### 7. Prefetch depth

Recommended initial settings:

- extraction ahead: 1 or 2 packages;
- option records prepared ahead: up to 4 packages;
- one package actively mutating/applying options;
- one BSA process active;
- no more than 1 large archive extraction.

For smaller packages, prefetch 3-4. For packages over 2 GB, prefetch only 1.

The coordinator should expose a `PackageWeight` based on:

- archive size;
- file count;
- expanded-size estimate;
- current disk pressure;
- whether it contains a BSA;
- whether earlier Tidy was expensive.

### 8. Priority ordering

The option dialog order is deterministic:

1. configured major-master order;
2. required dependencies before consumers when already known;
3. companion/regular plugin dependencies appended after current major-master front;
4. retry-blocked packages after new prerequisites exist;
5. stable archive name as final tie-breaker.

Background work does not alter visible option order. A package can finish extracting early, but its dialog appears only when all earlier planned packages are not awaiting a prerequisite preparation.

## Detailed sequence

Assume a batch contains A, B, C:

```text
Coordinator:
  A -> EXTRACTING
  B -> QUEUED_FOR_EXTRACTION
  C -> QUEUED_FOR_EXTRACTION

A extraction completes:
  A -> PREPARING_OPTIONS
  B -> EXTRACTING

A option data is ready:
  GUI opens A dialog
  B continues extracting

User answers A:
  A -> OPTIONS_ANSWERED -> TIDY_PLANNING
  if B extraction is complete and options prepared:
      GUI immediately opens B dialog
  A Tidy runs in one background mutation slot

User answers B:
  B -> OPTIONS_ANSWERED
  A Tidy and B option-independent prep can continue
  B waits in QUEUED_FOR_TIDY because only one mutation slot is allowed
  C extracts/prepares

GUI opens C dialog when ready

A Tidy completes:
  B begins Tidy
  A discovers dependencies from selected options
  dependency archives are appended

Batch completes when:
  all original packages and discovered dependencies are READY or explicitly BLOCKED
```

The user experiences three consecutive option dialogs without waiting through each package's long Tidy.

## Dependency queue

Dependencies created by selected options must be first-class batch items.

Examples:

- OAAB Data -> Animated Containers;
- OAAB Data -> Epic Plants;
- Wares Ultimate -> NOD - Core;
- Glow in the Dahrk -> Dark Molag Mar.

For each dependency, record:

- display name;
- canonical Nexus mod ID when known;
- plugin filename when known;
- source package that requested it;
- option key;
- selected/not selected;
- archive candidates;
- install priority;
- whether it is a major master, regular plugin, asset-only mod, or companion.

### Resolver behavior

1. Check active load order.
2. Check complete staged packages.
3. Check managed archive downloads.
4. Use exact known Nexus identity before fuzzy name matching.
5. If no archive exists, open the exact Nexus page when known; otherwise open a keyword search.
6. Leave the dependency `WAITING_ON_DOWNLOAD`.
7. When the download appears, append it automatically on the next poll/resume.

### Wrong-archive prevention

The dependency resolver must support a canonical identity list:

```python
KNOWN_NEXUS_MODS = {
    "animated containers": 42238,
    "dark molag mar": 32101,
    "npc outfit diversity": 52091,
}
```

Exact mod IDs outrank token similarity.

For example, “Some Improved Animated Containers” (44321) is an optional mesh override and must never satisfy “Animated Containers” (42238).

## FOMOD preparation

Refactor option collection into two functions:

```python
prepare_options(package) -> OptionCatalog
submit_options(package, selections) -> None
```

Today, parts of option discovery and option presentation are coupled inside `process_staging`.

### `prepare_options`

Runs in a worker:

- discover FOMOD and BAIN options;
- parse XML;
- read local docs;
- calculate plugin masters and dependencies;
- identify option files;
- attach maintainer notes;
- resolve preview images;
- load saved answers;
- produce a JSON-serializable `OptionCatalog`.

It must not:

- create Tk widgets;
- write selected choices;
- mutate the payload.

### `submit_options`

Runs as a coordinator transition:

- validate answer keys still exist;
- validate the package marker/hash has not changed;
- persist answer JSON atomically;
- set `OPTIONS_ANSWERED`;
- enqueue Tidy.

## Tidy process isolation

Each package should keep:

- its own source folder;
- its own plan file;
- its own journal scope;
- its own answer file;
- its own output log.

Global output paths are currently one shared concern. The batch version should pass an explicit package work id/environment:

```text
MWMANAGER_BATCH_ID
MWMANAGER_PACKAGE_ID
MWMANAGER_PACKAGE_NAME
MWMANAGER_PLAN_PATH
MWMANAGER_PROGRESS_PREFIX
```

Prefer a batch output structure:

```text
outputs/batches/<batch-id>/
  batch.json
  events.jsonl
  <package-id>/
    plan.txt
    progress.log
    options.json
    tidy.jsonl
```

Do not rely only on the global `mwmanager_vortex_tidy_plan.txt`, because parallel preparation cannot safely share it.

## Cancellation and recovery

### Cooperative cancel

“Stop after current operation”:

- stops scheduling new packages;
- lets the active Tidy/file operation finish its current transaction;
- keeps all extracted/prepared packages;
- records each unfinished package phase;
- does not delete answers.

### Force stop

“Force stop”:

- terminates owned child processes;
- leaves durable transaction records;
- does not claim package completion;
- makes every package re-enter recovery at next launch.

### Resume

At GUI startup:

1. load active batch manifest;
2. recover durable transactions using each journal's committed/rolled-back marker: complete committed transitions, roll back uncommitted file mutations, and mark indeterminate journals `BLOCKED_USER_ACTION`;
3. remove temporary `.extract-*`/`.replaced-*` only after transaction validation;
4. preserve completed option answers;
5. rebuild queued FOMOD work from persisted `FOMOD_DISCOVERED` events, excluding only packages with saved valid answers;
6. enqueue `OPTIONS_ANSWERED` packages back to Tidy;
7. reevaluate dependencies whose source archives have appeared;
8. show one recovery summary, not one popup per package.

For each persisted `FOMOD_DISCOVERED` event, verify the package hash, installer config hash, and payload fingerprint before rebuilding its `OptionsReady` item. A valid answered discovery is marked delivered/complete. An invalidated discovery remains recorded as historical but is not shown.

## Progress UX

The user should always see:

- current option: `A of N`;
- prepared-ahead count;
- packages still extracting;
- packages Tidy-complete;
- packages waiting on dependencies;
- current package/phase;
- last meaningful event.

Recommended footer:

```text
FOMOD 3 of 8
Prepared: 5  |  Running Tidy: 2  |  Extracting: 1  |  Waiting on mods: 4
Current: Glow in the Dahrk — Applying selected options
```

Progress updates:

- phase changes always;
- package transitions always;
- file counts every 2,000 operations;
- otherwise a 30-second heartbeat;
- one summary line, not a log entry for every file.

## UI workflow

### Opening

The batch window paints first, then:

1. show a short modal “Preparing FOMOD options…” while initial package extraction/inspection starts;
2. close the modal after the first `OptionsReady`;
3. display that FOMOD dialog;
4. keep a compact batch status header visible behind/alongside it.

### Between options

After Save/Next:

1. persist the answers;
2. immediately show “Preparing next option set…” if the next catalog is not ready;
3. otherwise open the next selector without waiting for Tidy;
4. show failures/blocked states in the status header rather than interrupting healthy packages.

If the coordinator accepts a `FomodDiscovered` event while a selector is open, persist it and display a compact “New FOMOD ready; it will open next if still highest priority” indicator. Do not send the worker event directly to the selector and do not replace the active modal. After the active answers are saved, open the highest-priority eligible `OptionsReady` item.

### End of option collection

When all currently discovered option windows are answered:

- continue background Tidy;
- show “N packages processing, M blocked on dependencies”;
- do not block the main window with one long synchronous job.

## Safety invariants

1. A package has exactly one package-mutation worker.
2. A child process is always registered in `managed_processes`.
3. A package answer applies only to the archive/package hash it was prepared against.
4. A stale plan is refused, retried, or explicitly approved; it is never silently force-applied.
5. A package is not `READY_FOR_DEPLOYMENT` until Tidy, selected options, asset work, and BSA checks complete.
6. A dependency archive cannot replace a package with the same normalized name unless explicitly ranked by canonical mod identity.
7. A cancellation never marks an unfinished phase complete.
8. A user-declined optional component is recorded as declined and can be reoffered later when its dependency appears.
9. Live `Data Files` deployment remains outside this batch coordinator until a user confirms final deploy.
10. SQLite/catalog preparation is serialized even if other file work is concurrent.
11. A FOMOD discovery event is persisted before it is published to the Tk queue.
12. A live FOMOD dialog is never interrupted to open a later discovery.
13. Activating an option payload rescans for nested installers before marking that package ready.
14. Background work on unrelated packages continues while the UI is waiting for user input.

## Performance expectations

For a ten-package batch where:

- five packages have long extraction/Tidy work;
- seven FOMOD windows require answers;

the current flow takes approximately:

```text
extract A + answer A + tidy A
+ extract B + answer B + tidy B
+ ...
```

The proposed flow takes approximately:

```text
answer A, B, C, ... with extraction/Tidy overlapped
+ remaining max(Tidy tail, dependency downloads)
```

The CPU/disk phase may be similar in total, but the user’s waiting time is grouped: choices first, processing after/alongside. The application remains visibly active throughout.

## Failure handling

### One package fails

- mark only that package `FAILED_RETRYABLE` or `BLOCKED_USER_ACTION`;
- continue option collection for other packages;
- allow retry/remove/open-plan actions from batch view;
- do not abort the whole batch unless the error is a shared database/transaction/state failure.

### Discovered FOMOD cannot be parsed

- persist the discovery and parser error without emitting `OptionsReady`;
- mark only that package `BLOCKED_USER_ACTION` with the installer path and expected config location;
- remove its pending interactive item, but keep unrelated work running;
- offer **Retry**, **Open installer folder**, and **Skip package** from batch status;
- retry only the affected package and create a new attempt number while retaining the previous error history.

### Shared-state failure

For:

- corrupted batch manifest;
- SQLite lock/contention;
- disk full;
- manager mutation lock failure;
- invalid selected archive identity;

stop new scheduling and enter `BATCH_BLOCKED`.

### Duplicate dependency

Deduplicate discoveries and dependencies by an idempotency key containing:

```text
archive sha256
canonical Nexus mod id
normalized plugin identity
normalized installer path
config sha256
selected payload fingerprint
```

Use the identity precedence:

1. exact archive hash;
2. exact canonical Nexus mod ID;
3. exact installer path/config hash for an already known package;
4. normalized plugin identity only as a fallback.

Conflicting hashes under one canonical Nexus ID are never silently merged. Choose exact known Nexus ID before fuzzy name matching, and route conflicting content to `BLOCKED_USER_ACTION` for explicit review.

## Migration from current workflow

Phase 1 can wrap existing code without a rewrite:

1. Create batch manifest and coordinator.
2. Move archive extraction behind the coordinator.
3. Run option discovery in a worker but keep the current selector UI.
4. Submit choices to existing `mock_fomod` activation path.
5. Continue using current Tidy CLI and package marker state.
6. Add UI events and queue status.

Phase 2:

1. split plan preparation from plan apply at package scope;
2. replace global plan paths with batch package paths;
3. allow package B/C option collection while package A runs Tidy.

Phase 3:

1. enable bounded worker concurrency;
2. add dependency package queuing;
3. add package priority/weight scheduling;
4. add batch history and retry UI.

## Recommended first implementation

Implement a minimal version with:

- 1 extraction/preparation thread;
- 1 mutation/Tidy worker;
- 1 Tk option dispatcher;
- durable package queue;
- no same-package parallelism;
- no parallel SQLite work.

This is enough to move nearly all FOMOD interactions to the front. General concurrency can come after measurements.

## Testing plan

### Unit tests

- legal state transitions only;
- deterministic option order;
- answer hash/package hash validation;
- dependency deduplication;
- exact Nexus-ID resolution;
- declined option remains remembered;
- package retry preserves completed phases;
- stale options are rejected;
- discovery events are idempotent;
- an invalidated persisted discovery is not republished;
- the 16-level nested-installer limit blocks only the affected package.

### Failure injection

- terminate during extraction;
- terminate during FOMOD preparation;
- terminate after answer save, before Tidy;
- terminate during Tidy apply;
- child process hangs without output;
- plan changes before apply;
- disk full after N packages;
- archive path disappears after extraction;
- duplicate archive added by two dependencies;
- wrong archive with similar name;
- FOMOD discovered while another selector is open;
- FOMOD discovered inside a newly activated optional payload;
- repeated discovery of the same installer;
- crash after discovery persistence but before GUI publication;
- dynamically discovered FOMOD with an invalid/corrupt config;
- conflicting identities for one canonical Nexus ID.

### GUI tests

- window paints before scan begins;
- modal scan closes after worker completion;
- only one FOMOD dialog is open;
- next FOMOD opens without waiting for prior package's Tidy;
- cancellation does not lose saved answers;
- Done resumes waiting dependencies;
- queue status remains accurate after restart;
- newly discovered FOMOD opens immediately after the current answer is saved;
- an open FOMOD retains unsaved state when another discovery arrives;
- extraction/Tidy progress continues while a FOMOD is open;
- nested discoveries are handled repeatedly without duplicate dialogs;
- a corrupt newly discovered FOMOD blocks only its package;
- reload discards unsaved choices but preserves previously saved answers;
- closing the active selector does not allow a queued discovery to overwrite its saved state.

### Performance tests

- 100k-file synthetic package;
- 10-package batch with fast and slow packages;
- multiple 2+ GB archives;
- one unavailable dependency;
- repeated `OptionsReady` events;
- burst of dynamically discovered FOMOD events;
- memory stability across 1,000 packages/events.

## Metrics

Record:

- seconds from batch start to first option dialog;
- seconds between option dialogs;
- seconds from last option dialog to ready-for-deploy;
- package extraction/Tidy/apply duration;
- files linked/deployed;
- canceled/retried package count;
- queue wait time;
- dependency resolution accuracy;
- exact archive selected versus fuzzy candidate.

The first success criterion is:

> Consecutive FOMOD dialogs are no longer separated by each package's full Tidy/apply duration.

The dynamic-discovery success criteria are:

> A FOMOD found during background work is shown next without losing the active dialog's state, while unrelated packages continue making progress.

## Decisions required before implementation

1. Is one active batch sufficient, or must MWManager retain parallel regular-plugin and major-master batches?
2. May the user close the batch window while workers continue, or must workers stop at a safe boundary?
3. Should dependency downloads auto-open the browser but continue other packages, or pause the batch at each missing download?
4. Should completed FOMOD answers be immutable for the batch, or editable until the package applies?
5. Is a hidden/advanced “parallelism” setting desired, or should the coordinator choose conservative limits automatically?

Recommended answers:

1. one active batch;
2. closing the window requests safe stop, not continued background mutation;
3. continue other packages while missing downloads stay `WAITING_ON_DOWNLOAD`;
4. editable until that package enters `TIDY_APPLYING`, immutable afterward;
5. automatic limits initially; optional diagnostic override later.

## Final recommendation

Build the queue and state machine first, then add a single preparation thread and one mutation worker. Do not begin with a free-form “one thread per archive” model.

The immediate user-facing milestone is:

- all possible FOMOD choices are prepared and shown in sequence;
- extraction/Tidy work continues behind the chooser;
- long work never blocks Tk;
- a crash resumes the same package phase;
- discovered dependencies join the same queue.

This gives the desired UX without sacrificing MWManager's strongest existing property: durable, recoverable file operations.

"""Durable, threaded batching for archive preparation and serialized option collection."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import heapq
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid
from typing import Callable
from workflow_safety import atomic_json


ACTIVE_MANIFEST = Path("outputs") / "mwmanager_active_batch.json"
MANIFEST_VERSION = 1
MAX_DISCOVERY_DEPTH = 16


PHASES = {
    "DISCOVERED",
    "QUEUED_FOR_EXTRACTION",
    "EXTRACTING",
    "EXTRACTED",
    "QUEUED_FOR_OPTION_PREP",
    "PREPARING_OPTIONS",
    "AWAITING_OPTIONS",
    "OPTIONS_ANSWERED",
    "QUEUED_FOR_TIDY",
    "TIDY_PLANNING",
    "TIDY_APPLYING",
    "TIDY_COMPLETE",
    "DEPENDENCY_DISCOVERY",
    "WAITING_ON_DEPENDENCIES",
    "READY_FOR_DEPLOYMENT",
    "BLOCKED_USER_ACTION",
    "FAILED_RETRYABLE",
    "CANCELLED",
}

TRANSITIONS = {
    "DISCOVERED": {"QUEUED_FOR_EXTRACTION", "BLOCKED_USER_ACTION", "FAILED_RETRYABLE", "CANCELLED"},
    "QUEUED_FOR_EXTRACTION": {"EXTRACTING", "FAILED_RETRYABLE", "BLOCKED_USER_ACTION", "CANCELLED"},
    "EXTRACTING": {"EXTRACTED", "FAILED_RETRYABLE", "BLOCKED_USER_ACTION", "CANCELLED"},
    "EXTRACTED": {"QUEUED_FOR_OPTION_PREP", "PREPARING_OPTIONS", "FAILED_RETRYABLE",
                 "BLOCKED_USER_ACTION", "CANCELLED"},
    "QUEUED_FOR_OPTION_PREP": {"PREPARING_OPTIONS", "FAILED_RETRYABLE", "BLOCKED_USER_ACTION", "CANCELLED"},
    "PREPARING_OPTIONS": {"AWAITING_OPTIONS", "OPTIONS_ANSWERED", "FAILED_RETRYABLE",
                          "BLOCKED_USER_ACTION", "CANCELLED"},
    "AWAITING_OPTIONS": {"PREPARING_OPTIONS", "OPTIONS_ANSWERED", "BLOCKED_USER_ACTION",
                         "FAILED_RETRYABLE", "CANCELLED"},
    "OPTIONS_ANSWERED": {"QUEUED_FOR_TIDY", "TIDY_PLANNING", "TIDY_COMPLETE", "BLOCKED_USER_ACTION",
                         "FAILED_RETRYABLE", "CANCELLED"},
    "QUEUED_FOR_TIDY": {"TIDY_PLANNING", "FAILED_RETRYABLE", "BLOCKED_USER_ACTION", "CANCELLED"},
    "TIDY_PLANNING": {"TIDY_APPLYING", "TIDY_COMPLETE", "PREPARING_OPTIONS",
                      "FAILED_RETRYABLE", "BLOCKED_USER_ACTION", "CANCELLED"},
    "TIDY_APPLYING": {"TIDY_COMPLETE", "PREPARING_OPTIONS", "FAILED_RETRYABLE",
                      "BLOCKED_USER_ACTION", "CANCELLED"},
    "TIDY_COMPLETE": {"DEPENDENCY_DISCOVERY", "PREPARING_OPTIONS", "READY_FOR_DEPLOYMENT",
                      "WAITING_ON_DEPENDENCIES", "FAILED_RETRYABLE", "BLOCKED_USER_ACTION"},
    "DEPENDENCY_DISCOVERY": {"WAITING_ON_DEPENDENCIES", "READY_FOR_DEPLOYMENT",
                             "FAILED_RETRYABLE", "BLOCKED_USER_ACTION"},
    "WAITING_ON_DEPENDENCIES": {"DEPENDENCY_DISCOVERY", "READY_FOR_DEPLOYMENT",
                                "FAILED_RETRYABLE", "BLOCKED_USER_ACTION", "CANCELLED"},
    "FAILED_RETRYABLE": {"QUEUED_FOR_EXTRACTION", "QUEUED_FOR_OPTION_PREP", "QUEUED_FOR_TIDY",
                         "BLOCKED_USER_ACTION", "CANCELLED"},
    "BLOCKED_USER_ACTION": {"QUEUED_FOR_EXTRACTION", "QUEUED_FOR_OPTION_PREP", "QUEUED_FOR_TIDY",
                            "CANCELLED"},
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _identifier(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _phase_package() -> dict:
    return {
        "phase": "DISCOVERED",
        "attempts": 0,
        "worker_slot": None,
        "last_error": None,
        "history": [],
        "discoveries": [],
        "answer_sha256": None,
    }


@dataclass(frozen=True)
class OptionsReady:
    package_id: str
    discovery_id: str
    priority: int
    sequence: int
    option_records: dict


class PromptGate:
    """Serialize option subprocesses, allowing newly discovered installers to jump the queue."""

    def __init__(self):
        self._condition = threading.Condition()
        self._held = False
        self._sequence = 0
        self._waiting: list[tuple[int, int, str]] = []

    def acquire(self, package_id: str, priority: int) -> None:
        with self._condition:
            self._sequence += 1
            ticket = (int(priority), self._sequence, str(package_id))
            heapq.heappush(self._waiting, ticket)
            while self._held or self._waiting[0] != ticket:
                self._condition.wait()
            heapq.heappop(self._waiting)
            self._held = True

    def release(self) -> None:
        with self._condition:
            self._held = False
            self._condition.notify_all()

    def __call__(self, package_id: str, priority: int):
        gate = self

        class _Context:
            def __enter__(self):
                gate.acquire(package_id, priority)
                return gate

            def __exit__(self, exc_type, exc_value, traceback):
                _ = (exc_type, exc_value, traceback)
                gate.release()
                return False

        return _Context()


class BatchCoordinator:
    """Manifest-backed state and event coordinator; it contains no Tk code."""

    def __init__(self, manager_dir: str | os.PathLike, manifest: dict):
        self.manager_dir = Path(manager_dir).resolve()
        self.manifest = manifest
        self.path = self.manager_dir / ACTIVE_MANIFEST
        self.event_log = self.manager_dir / "outputs" / "batches" / \
            str(manifest.get("batch_id") or "batch") / "events.jsonl"
        self._lock = threading.RLock()
        self._option_sequence = max(
            (int(item.get("sequence") or 0) for item in manifest.get("ready_queue", [])),
            default=0,
        )
        self.cancel_requested = bool(manifest.get("cancel_requested"))
        self._save()

    @classmethod
    def start(cls, manager_dir: str | os.PathLike, game_dir: str | os.PathLike,
              packages: list[dict], *, kind: str = "major_masters") -> "BatchCoordinator":
        batch_id = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        manifest = {
            "version": MANIFEST_VERSION,
            "batch_id": batch_id,
            "kind": kind,
            "phase": "collecting_options",
            "created_at": _now(),
            "updated_at": _now(),
            "game_dir": str(Path(game_dir).resolve()),
            "packages": {},
            "ready_queue": [],
            "discoveries": {},
            "cancel_requested": False,
        }
        for package in packages:
            package_id = str(package.get("id") or package.get("name") or _identifier(str(package)))
            manifest["packages"][package_id] = {**_phase_package(), **package, "id": package_id,
                                                "phase": "QUEUED_FOR_EXTRACTION"}
        return cls(manager_dir, manifest)

    @classmethod
    def resume(cls, manager_dir: str | os.PathLike) -> "BatchCoordinator | None":
        path = Path(manager_dir).resolve() / ACTIVE_MANIFEST
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if int(manifest.get("version") or 0) != MANIFEST_VERSION:
            return None
        if not isinstance(manifest.get("packages"), dict):
            return None
        coordinator = cls(manager_dir, manifest)
        with coordinator._lock:
            coordinator._rebuild_ready_queue()
            coordinator._save()
        return coordinator

    def _save(self) -> None:
        self.manifest["updated_at"] = _now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        backup = self.path.with_suffix(self.path.suffix + ".bak")
        if self.path.is_file():
            try:
                backup.write_bytes(self.path.read_bytes())
            except OSError:
                pass
        atomic_json(self.path, self.manifest)

    def _emit(self, event: str, **data) -> None:
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        payload = {"event": event, "at": _now(), **data}
        with self.event_log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def package(self, package_id: str) -> dict:
        return self.manifest["packages"][package_id]

    def discovery_state(self, discovery_id: str) -> str:
        with self._lock:
            return str(self.manifest["discoveries"][discovery_id]["state"])

    def add_package(self, package_id: str, **details) -> dict:
        with self._lock:
            if package_id in self.manifest["packages"]:
                return self.manifest["packages"][package_id]
            entry = {**_phase_package(), **details, "id": package_id, "phase": "QUEUED_FOR_EXTRACTION"}
            self.manifest["packages"][package_id] = entry
            self._save()
            return entry

    def transition(self, package_id: str, phase: str, *, worker_slot: str | None = None,
                   error: str | None = None, **details) -> dict:
        if phase not in PHASES:
            raise ValueError(f"Unknown batch phase: {phase}")
        with self._lock:
            package = self.package(package_id)
            previous = package["phase"]
            if phase != previous and phase not in TRANSITIONS.get(previous, set()):
                raise ValueError(f"Illegal batch transition for {package_id}: {previous} -> {phase}")
            now = _now()
            if phase in {"EXTRACTING", "PREPARING_OPTIONS", "TIDY_PLANNING", "TIDY_APPLYING"}:
                package["attempts"] = int(package.get("attempts") or 0) + 1
            package.update(details)
            package.update(phase=phase, worker_slot=worker_slot, last_error=str(error)[:2000] if error else None)
            package.setdefault("history", []).append({
                "at": now, "from": previous, "to": phase, "attempts": package["attempts"],
                "worker_slot": worker_slot, "error": package["last_error"],
            })
            self._save()
            self._emit("PackageTransition", package_id=package_id, phase=phase, previous=previous)
            return package

    @staticmethod
    def discovery_key(package_id: str, installer_path: str, config_hash: str,
                      payload_fingerprint: str = "") -> str:
        raw = "\0".join((package_id, installer_path.replace("\\", "/").casefold(),
                         config_hash, payload_fingerprint))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def discover_fomod(self, package_id: str, *, installer_path: str, config_hash: str,
                       option_records: dict, source: str, priority: int = 50,
                       depth: int = 0, payload_fingerprint: str = "") -> str | None:
        if depth > MAX_DISCOVERY_DEPTH:
            self.transition(package_id, "BLOCKED_USER_ACTION",
                            error=f"Nested FOMOD depth exceeded {MAX_DISCOVERY_DEPTH}.")
            return None
        discovery_id = self.discovery_key(package_id, installer_path, config_hash, payload_fingerprint)
        with self._lock:
            if discovery_id in self.manifest["discoveries"]:
                return discovery_id
            discovery = {
                "id": discovery_id,
                "package_id": package_id,
                "installer_path": installer_path,
                "config_hash": config_hash,
                "payload_fingerprint": payload_fingerprint,
                "option_records": option_records,
                "source": source,
                "priority": int(priority),
                "depth": int(depth),
                "state": "discovered",
                "discovered_at": _now(),
            }
            self.manifest["discoveries"][discovery_id] = discovery
            package = self.package(package_id)
            package.setdefault("discoveries", []).append(discovery_id)
            self._save()  # durable before the UI event
            self._emit("FOMOD_DISCOVERED", discovery_id=discovery_id, package_id=package_id,
                       priority=int(priority), source=source)
            self.publish_options(discovery_id)
            return discovery_id

    def publish_options(self, discovery_id: str) -> None:
        with self._lock:
            discovery = self.manifest["discoveries"][discovery_id]
            if discovery["state"] in {"ready", "answered", "invalidated"}:
                return
            self._option_sequence += 1
            ready = {
                "kind": "OptionsReady",
                "package_id": discovery["package_id"],
                "discovery_id": discovery_id,
                "priority": discovery["priority"],
                "sequence": self._option_sequence,
                "ready_at": _now(),
            }
            self.manifest["ready_queue"].append(ready)
            discovery["state"] = "ready"
            package_id = discovery["package_id"]
            if self.package(package_id)["phase"] in {"EXTRACTED", "QUEUED_FOR_OPTION_PREP"}:
                self.transition(package_id, "PREPARING_OPTIONS", worker_slot="tk-options")
            self._save()
            self._emit("OptionsReady", **ready)

    def next_options(self) -> OptionsReady | None:
        with self._lock:
            ready = [item for item in self.manifest["ready_queue"] if item.get("state", "pending") == "pending"]
            if not ready:
                return None
            item = min(ready, key=lambda value: (int(value["priority"]), int(value["sequence"])))
            item["state"] = "active"
            self._save()
            discovery = self.manifest["discoveries"][item["discovery_id"]]
            return OptionsReady(
                package_id=item["package_id"], discovery_id=item["discovery_id"],
                priority=int(item["priority"]), sequence=int(item["sequence"]),
                option_records=discovery["option_records"],
            )

    def submit_answers(self, discovery_id: str, answer_payload: dict) -> None:
        answer_hash = hashlib.sha256(
            json.dumps(answer_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        with self._lock:
            discovery = self.manifest["discoveries"][discovery_id]
            discovery.update(state="answered", answer_sha256=answer_hash, answered_at=_now())
            package = self.package(discovery["package_id"])
            package["answer_sha256"] = answer_hash
            for item in self.manifest["ready_queue"]:
                if item.get("discovery_id") == discovery_id:
                    item["state"] = "answered"
            self._save()
            self._emit("OptionsAnswered", discovery_id=discovery_id,
                       package_id=discovery["package_id"], answer_sha256=answer_hash)

    def cancel(self) -> None:
        with self._lock:
            self.cancel_requested = True
            self.manifest["cancel_requested"] = True
            self._save()

    def mark_package_error(self, package_id: str, error: BaseException, *,
                           blocked: bool = False) -> None:
        current = self.package(package_id)["phase"]
        if current == ("BLOCKED_USER_ACTION" if blocked else "FAILED_RETRYABLE"):
            return
        self.transition(package_id, "BLOCKED_USER_ACTION" if blocked else "FAILED_RETRYABLE", error=str(error))

    def status(self) -> dict:
        with self._lock:
            counts: dict[str, int] = {}
            for package in self.manifest["packages"].values():
                counts[package["phase"]] = counts.get(package["phase"], 0) + 1
            return {
                "batch_id": self.manifest["batch_id"],
                "phase": self.manifest["phase"],
                "cancel_requested": self.cancel_requested,
                "packages": len(self.manifest["packages"]),
                "phase_counts": counts,
                "options_pending": sum(
                    item.get("state", "pending") == "pending" for item in self.manifest["ready_queue"]
                ),
            }

    def _rebuild_ready_queue(self) -> None:
        retained = []
        discoveries = self.manifest.setdefault("discoveries", {})
        for item in self.manifest.get("ready_queue", []):
            discovery = discoveries.get(item.get("discovery_id"))
            if not discovery or discovery.get("state") in {"answered", "invalidated"}:
                continue
            if item.get("state") == "active":
                item["state"] = "pending"
            retained.append(item)
        self.manifest["ready_queue"] = retained

    def finish(self) -> Path:
        with self._lock:
            self.manifest["phase"] = "complete"
            self._save()
            destination = self.manager_dir / "outputs" / "batches" / \
                str(self.manifest["batch_id"]) / "manifest.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(destination, self.manifest)
            self.path.unlink(missing_ok=True)
            self.path.with_suffix(self.path.suffix + ".bak").unlink(missing_ok=True)
            self._emit("BatchComplete", batch_id=self.manifest["batch_id"])
            return destination


def package_id_for(package: str | os.PathLike) -> str:
    path = Path(package)
    return f"{path.name.casefold()}-{_identifier(str(path.resolve()))}"


def read_json_file(path: str | os.PathLike) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def file_sha256(path: str | os.PathLike) -> str | None:
    try:
        with Path(path).open("rb") as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            return digest.hexdigest()
    except OSError:
        return None


@dataclass
class BatchUnit:
    package_id: str
    path: Path
    context: dict


def run_threaded_batch(
    coordinator: BatchCoordinator,
    items: list[dict],
    *,
    extract: Callable[[dict, int], BatchUnit],
    prepare_options: Callable[[BatchUnit, bool], None],
    apply: Callable[[BatchUnit], None],
    fingerprint: Callable[[BatchUnit], str | None],
    config_fingerprint: Callable[[BatchUnit], str],
    reset_after_option_change: Callable[[BatchUnit], None],
    on_ready: Callable[[BatchUnit], None],
    progress: Callable[[str], None],
    max_discovery_depth: int = MAX_DISCOVERY_DEPTH,
) -> list[BatchUnit]:
    """Run extraction ahead, serialize option selection, and apply answered packages concurrently."""
    notifications: queue.Queue[tuple] = queue.Queue()
    tidy_queue: queue.Queue[tuple | None] = queue.Queue()
    errors: queue.Queue[BaseException] = queue.Queue()
    finished: set[str] = set()
    package_errors: list[str] = []
    staged: dict[str, BatchUnit] = {}
    result: list[BatchUnit] = []
    dynamic_waiters: dict[str, threading.Event] = {}
    answered_dynamic: set[str] = set()
    cancel_check = getattr(progress, "check_cancelled", None)

    def checkpoint() -> None:
        if cancel_check:
            cancel_check()
        if coordinator.cancel_requested:
            raise RuntimeError("Batch cancellation was requested.")

    def extract_worker() -> None:
        try:
            for index, item in enumerate(items, 1):
                checkpoint()
                package_id = item["id"]
                coordinator.transition(package_id, "EXTRACTING", worker_slot="extract")
                progress(f"Preparing archive {index} of {len(items)}: {item.get('name', package_id)}")
                unit = extract(item, index)
                if unit.package_id != package_id:
                    raise RuntimeError(f"Extractor returned the wrong package: {unit.package_id}")
                staged[package_id] = unit
                result.append(unit)
                coordinator.transition(
                    package_id, "EXTRACTED", worker_slot="extract",
                    staging_path=str(unit.path.resolve()),
                    archive_sha256=config_fingerprint(unit),
                )
                coordinator.discover_fomod(
                    package_id,
                    installer_path=str(unit.path),
                    config_hash=config_fingerprint(unit),
                    option_records={"kind": "initial", "context": unit.context},
                    source="batch_extraction",
                    priority=index * 10,
                )
                notifications.put(("options_ready", package_id))
        except BaseException as exc:
            active = next((package_id for package_id, package in coordinator.manifest["packages"].items()
                           if package.get("phase") == "EXTRACTING"), None)
            if active:
                try:
                    coordinator.mark_package_error(active, exc)
                except ValueError:
                    pass
            errors.put(exc)
        finally:
            notifications.put(("extraction_done", None))

    def tidy_worker() -> None:
        while True:
            try:
                unit = tidy_queue.get(timeout=0.2)
            except queue.Empty:
                if coordinator.cancel_requested:
                    return
                continue
            if unit is None:
                return
            try:
                coordinator.transition(unit.package_id, "TIDY_PLANNING", worker_slot="tidy")
                coordinator.transition(unit.package_id, "TIDY_APPLYING", worker_slot="tidy")
                apply(unit)
                coordinator.transition(unit.package_id, "TIDY_COMPLETE", worker_slot="tidy")

                blocked = False
                for depth in range(max_discovery_depth + 1):
                    before = fingerprint(unit)
                    discovery_id = coordinator.discover_fomod(
                        unit.package_id,
                        installer_path=str(unit.path),
                        config_hash=config_fingerprint(unit),
                        option_records={"kind": "post_apply_rescan", "depth": depth,
                                        "context": unit.context},
                        source="post_apply_rescan",
                        priority=0,
                        depth=depth,
                        payload_fingerprint=str(before or ""),
                    )
                    if discovery_id is None:
                        break
                    if coordinator.discovery_state(discovery_id) == "answered":
                        coordinator.transition(unit.package_id, "TIDY_COMPLETE", worker_slot="tidy")
                        break
                    coordinator.transition(unit.package_id, "PREPARING_OPTIONS",
                                           worker_slot="tk-options")
                    waiter = threading.Event()
                    notifications.put(("dynamic_ready", discovery_id, waiter))
                    while not waiter.wait(0.2):
                        checkpoint()
                    if coordinator.package(unit.package_id)["phase"] == "BLOCKED_USER_ACTION":
                        blocked = True          # the options loop recorded it; the other queued units go on
                        break
                    if before == fingerprint(unit):
                        coordinator.transition(unit.package_id, "TIDY_COMPLETE", worker_slot="tidy")
                        break
                    reset_after_option_change(unit)
                    coordinator.transition(unit.package_id, "TIDY_PLANNING", worker_slot="tidy")
                    coordinator.transition(unit.package_id, "TIDY_APPLYING", worker_slot="tidy")
                    apply(unit)
                    coordinator.transition(unit.package_id, "TIDY_COMPLETE", worker_slot="tidy")
                else:
                    raise RuntimeError(f"{unit.path.name}: nested FOMOD discovery did not stabilize.")
                if blocked:
                    continue

                coordinator.transition(unit.package_id, "READY_FOR_DEPLOYMENT", worker_slot="tidy")
                on_ready(unit)
                finished.add(unit.package_id)
                notifications.put(("package_done", unit.package_id))
            except BaseException as exc:
                try:
                    coordinator.mark_package_error(unit.package_id, exc)
                except ValueError:
                    pass
                package_errors.append(f"{unit.path.name}: {exc}")
                finished.add(unit.package_id)
                notifications.put(("package_error", unit.package_id))

    extraction_thread = threading.Thread(
        target=extract_worker, name="MWManager batch extract", daemon=True)
    tidy_thread = threading.Thread(
        target=tidy_worker, name="MWManager batch tidy", daemon=True)
    extraction_thread.start()
    tidy_thread.start()

    try:
        while len(finished) < len(items):
            checkpoint()
            try:
                raise errors.get_nowait()
            except queue.Empty:
                pass

            for _ in range(100):
                try:
                    event = notifications.get_nowait()
                except queue.Empty:
                    break
                if event[0] == "dynamic_ready":
                    discovery_id, waiter = event[1], event[2]
                    dynamic_waiters[discovery_id] = waiter
                    if discovery_id in answered_dynamic:
                        waiter.set()
                        answered_dynamic.discard(discovery_id)

            option = coordinator.next_options()
            if option is None:
                time.sleep(0.05)
                continue
            unit = staged.get(option.package_id)
            if unit is None:
                raise RuntimeError(f"Option event lost its staged package: {option.package_id}")
            records = option.option_records
            try:
                prepare_options(unit, records.get("kind") == "post_apply_rescan")
            except BaseException as exc:
                try:
                    coordinator.mark_package_error(unit.package_id, exc, blocked=True)
                except ValueError:
                    pass
                package_errors.append(f"{unit.path.name}: {exc}")
                finished.add(unit.package_id)
                if records.get("kind") == "post_apply_rescan":
                    waiter = dynamic_waiters.pop(option.discovery_id, None)
                    if waiter is None:
                        answered_dynamic.add(option.discovery_id)
                    else:
                        waiter.set()
                progress(f"{unit.path.name}: FOMOD blocked; other packages will continue ({exc}).")
                continue
            coordinator.submit_answers(
                option.discovery_id,
                {"fingerprint": fingerprint(unit), "depth": int(records.get("depth") or 0)},
            )
            coordinator.transition(option.package_id, "OPTIONS_ANSWERED", worker_slot="tk-options")
            if records.get("kind") == "post_apply_rescan":
                waiter = dynamic_waiters.pop(option.discovery_id, None)
                if waiter is None:
                    answered_dynamic.add(option.discovery_id)
                else:
                    waiter.set()
            else:
                coordinator.transition(option.package_id, "QUEUED_FOR_TIDY")
                tidy_queue.put(unit)
    except BaseException:
        coordinator.cancel()
        raise
    finally:
        tidy_queue.put(None)
        extraction_thread.join(timeout=5)
        tidy_thread.join(timeout=5)

    if package_errors:
        raise RuntimeError(
            "Major-master batch finished with package errors:\n" + "\n".join(package_errors))
    coordinator.finish()
    return result


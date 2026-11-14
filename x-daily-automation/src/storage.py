"""JSON state persistence with atomic writes, validation, and a run lock.

Files in the data directory:
  state.json      scan checkpoint, account cache, recent run history
  posts.json      every discovered post, keyed by immutable post ID
  campaigns.json  promotion-planning records, keyed by post ID

Safety rules:
  * Writes go to a temp file in the same directory, are fsynced, then
    os.replace()d over the target, so a crash never leaves a half-written file.
  * state.json (which holds the checkpoint) is written last, so the checkpoint
    only advances once the posts and plans it covers are on disk.
  * Corrupt or partially missing state is never silently reset. The run stops
    with instructions; ALLOW_STATE_REINIT=true quarantines the bad file and
    starts that file fresh.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import format_iso, parse_iso_datetime

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STATE_FILE = "state.json"
POSTS_FILE = "posts.json"
CAMPAIGNS_FILE = "campaigns.json"
LOCK_FILE = ".run.lock"
RUN_HISTORY_LIMIT = 60
LOCK_STALE_SECONDS = 3 * 3600


class StateError(Exception):
    """Base class for state problems."""


class StateCorruptError(StateError):
    """A state file exists but is unreadable or fails validation."""


class PersistenceError(StateError):
    """State could not be written to disk."""


class LockError(StateError):
    """Another run holds the lock."""


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "account": {"user_id": None, "username": None},
        "last_successful_scan_at": None,
        "last_scan_window_start": None,
        "last_run": None,
        "run_history": [],
    }


def empty_posts() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "posts": {}}


def empty_campaigns() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "plans": {}}


# -- validation ----------------------------------------------------------


def _check_timestamp(value: Any, where: str, nullable: bool = True) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise StateCorruptError(f"{where} must be an ISO timestamp string")
    try:
        parse_iso_datetime(value)
    except ValueError as exc:
        raise StateCorruptError(f"{where} is not a valid ISO timestamp: {value!r}") from exc


def _check_version(data: Any, name: str) -> None:
    if not isinstance(data, dict):
        raise StateCorruptError(f"{name} must contain a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StateCorruptError(f"{name} has unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")


def validate_state(data: Any) -> None:
    _check_version(data, STATE_FILE)
    account = data.get("account")
    if not isinstance(account, dict):
        raise StateCorruptError("state.json 'account' must be an object")
    _check_timestamp(data.get("last_successful_scan_at"), "state.json last_successful_scan_at")
    _check_timestamp(data.get("last_scan_window_start"), "state.json last_scan_window_start")
    if not isinstance(data.get("run_history"), list):
        raise StateCorruptError("state.json 'run_history' must be a list")


def validate_posts(data: Any) -> None:
    _check_version(data, POSTS_FILE)
    posts = data.get("posts")
    if not isinstance(posts, dict):
        raise StateCorruptError("posts.json 'posts' must be an object keyed by post ID")
    for key, post in posts.items():
        where = f"posts.json post {key}"
        if not (isinstance(key, str) and key.isdigit()):
            raise StateCorruptError(f"{where}: key must be a numeric post ID")
        if not isinstance(post, dict) or post.get("post_id") != key:
            raise StateCorruptError(f"{where}: record must be an object whose post_id matches its key")
        if not isinstance(post.get("post_url"), str):
            raise StateCorruptError(f"{where}: post_url missing")
        if not isinstance(post.get("status"), str):
            raise StateCorruptError(f"{where}: status missing")
        _check_timestamp(post.get("created_at"), f"{where} created_at", nullable=False)
        _check_timestamp(post.get("discovered_at"), f"{where} discovered_at", nullable=False)


def validate_campaigns(data: Any) -> None:
    _check_version(data, CAMPAIGNS_FILE)
    plans = data.get("plans")
    if not isinstance(plans, dict):
        raise StateCorruptError("campaigns.json 'plans' must be an object keyed by post ID")
    for key, plan in plans.items():
        where = f"campaigns.json plan {key}"
        if not isinstance(plan, dict) or plan.get("post_id") != key:
            raise StateCorruptError(f"{where}: record must be an object whose post_id matches its key")
        if not isinstance(plan.get("targets"), dict):
            raise StateCorruptError(f"{where}: targets must be an object")
        if not isinstance(plan.get("authorized_campaigns", []), list):
            raise StateCorruptError(f"{where}: authorized_campaigns must be a list")


_FILES = {
    STATE_FILE: (validate_state, empty_state),
    POSTS_FILE: (validate_posts, empty_posts),
    CAMPAIGNS_FILE: (validate_campaigns, empty_campaigns),
}


# -- low-level IO ---------------------------------------------------------


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON so readers see either the old or the new file, never a mix."""
    path = Path(path)
    tmp_name: Optional[str] = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        with contextlib.suppress(OSError):  # directory fsync is best effort (not on all OSes)
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except (OSError, TypeError, ValueError) as exc:
        raise PersistenceError(f"Failed to write {path}: {type(exc).__name__}: {exc}") from exc
    finally:
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(f"{path.name} is not valid JSON (line {exc.lineno}): {exc.msg}") from exc
    except UnicodeDecodeError as exc:
        raise StateCorruptError(f"{path.name} is not valid UTF-8") from exc


# -- store ---------------------------------------------------------------


@dataclass
class BotState:
    state: dict[str, Any]
    posts: dict[str, Any]
    campaigns: dict[str, Any]
    first_run: bool = False
    recovery_notes: list[str] = field(default_factory=list)


class StateStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def path(self, name: str) -> Path:
        return self.data_dir / name

    def load(self, allow_reinit: bool = False) -> BotState:
        present = {name: self.path(name).exists() for name in _FILES}
        if not any(present.values()):
            log.warning("No state files found in %s: treating this as the FIRST RUN", self.data_dir)
            return BotState(empty_state(), empty_posts(), empty_campaigns(), first_run=True)

        loaded: dict[str, Any] = {}
        problems: dict[str, str] = {}
        for name, (validator, _) in _FILES.items():
            if not present[name]:
                problems[name] = f"{name} is missing while other state files exist"
                continue
            try:
                data = read_json(self.path(name))
                validator(data)
                loaded[name] = data
            except StateCorruptError as exc:
                problems[name] = str(exc)
            except OSError as exc:
                raise StateError(f"Cannot read {name}: {exc}") from exc

        notes: list[str] = []
        if problems:
            if not allow_reinit:
                details = "\n  - ".join(problems.values())
                raise StateCorruptError(
                    "State is invalid; refusing to continue so scan history is not lost:\n  - "
                    + details
                    + "\nFix the file (e.g. restore it with `git checkout <good-commit> -- data/`) "
                    "or re-run with ALLOW_STATE_REINIT=true to quarantine the bad file(s) and start them fresh."
                )
            for name, reason in problems.items():
                quarantined = self._quarantine(name) if present[name] else None
                loaded[name] = _FILES[name][1]()
                note = f"Reinitialized {name} ({reason})"
                if quarantined:
                    note += f"; previous copy kept at {quarantined}"
                log.error(note)
                notes.append(note)

        return BotState(loaded[STATE_FILE], loaded[POSTS_FILE], loaded[CAMPAIGNS_FILE], recovery_notes=notes)

    def _quarantine(self, name: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target_dir = self.data_dir / "quarantine"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}.{stamp}.corrupt"
        os.replace(self.path(name), target)
        return f"{self.data_dir.name}/quarantine/{target.name}"

    def save(self, bot_state: BotState) -> None:
        """Persist all files; state.json (checkpoint) last."""
        validate_posts(bot_state.posts)
        validate_campaigns(bot_state.campaigns)
        validate_state(bot_state.state)
        atomic_write_json(self.path(POSTS_FILE), bot_state.posts)
        atomic_write_json(self.path(CAMPAIGNS_FILE), bot_state.campaigns)
        atomic_write_json(self.path(STATE_FILE), bot_state.state)

    @contextlib.contextmanager
    def lock(self, stale_after: int = LOCK_STALE_SECONDS) -> Iterator[None]:
        """Exclusive run lock (guards local runs; Actions also uses a concurrency group)."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.path(LOCK_FILE)
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            age = time.time() - lock_path.stat().st_mtime
            if age < stale_after:
                raise LockError(
                    f"Another run appears to be in progress (lock file {lock_path} is {int(age)}s old). "
                    "If no run is active, delete the lock file."
                ) from None
            log.warning("Removing stale lock file (%ds old)", int(age))
            lock_path.unlink(missing_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "at": format_iso(datetime.now(timezone.utc))}))
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)


def record_run(state: dict[str, Any], run: dict[str, Any]) -> None:
    state["last_run"] = run
    history = state.setdefault("run_history", [])
    history.append(run)
    del history[:-RUN_HISTORY_LIMIT]

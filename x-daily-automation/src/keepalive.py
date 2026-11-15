"""Keeps a run alive so orders are placed when they are due.

GitHub starts scheduled workflows hours late and skips most of them, so the
timing of split view orders cannot depend on a fresh run starting every hour.
A run that still has something scheduled therefore stays alive: it sleeps until
the next action is due, runs one cycle, saves, and repeats. Before GitHub's
6-hour job limit it hands over to a fresh run, and it steps aside as soon as
another run (manual or scheduled) is waiting to start.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Optional

import requests

log = logging.getLogger(__name__)

POLL_SECONDS = 120  # how often a sleeping run checks whether another run is waiting
MIN_CYCLE_GAP = timedelta(minutes=5)  # two cycles never start closer together than this

SAVED, RESTART, FAILED = "saved", "restart", "failed"
RESTART_EXIT_CODE = 10  # the save command's way of saying "saved, but the code has changed"


def _saved() -> str:
    return SAVED


def _no() -> bool:
    return False


@dataclass
class Hooks:
    """What the loop needs from its surroundings; the defaults suit a plain local run."""

    persist: Callable[[], str] = _saved  # save state durably -> SAVED | RESTART | FAILED
    another_run_waiting: Callable[[], bool] = _no
    hand_over: Callable[[], bool] = _no  # start a fresh run; False if that was not possible


def run_loop(
    run_cycle: Callable[[bool], int],
    next_wake: Callable[[datetime], Optional[datetime]],
    hooks: Hooks,
    max_minutes: int,
    clock: Callable[[], datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run cycles until nothing is scheduled. Returns the process exit code.

    run_cycle(first) performs one scan/order cycle and returns its exit code.
    next_wake(now) says when the next cycle is needed, or None when there is
    nothing left to wait for. max_minutes limits how long this process lives
    (0 = no limit); past it the loop hands over instead of sleeping.
    """
    deadline = clock() + timedelta(minutes=max_minutes) if max_minutes else None
    first = True
    while True:
        cycle_started = clock()
        code = run_cycle(first)
        first = False
        if code not in (0, 1):
            return code  # configuration or state problem: waiting would not help

        saved = hooks.persist()
        if saved == FAILED:
            log.error("State could not be saved; stopping so that no order is placed without a record")
            return 1

        wake = next_wake(clock())
        if wake is None:
            log.info("Nothing is scheduled; ending this run")
            return code
        if saved == RESTART:
            log.info("The bot's code changed on the branch; handing over to a fresh run")
            return code if _hand_over(hooks) else 1

        wake = max(wake, cycle_started + MIN_CYCLE_GAP)
        if deadline is not None and wake >= deadline:
            log.info("Next action (%s) is past this run's time limit; handing over to a fresh run", wake.isoformat())
            return code if _hand_over(hooks) else 1

        log.info("Next action at %s; staying alive until then", wake.isoformat())
        while True:
            if hooks.another_run_waiting():
                log.info("Another run is waiting; ending this one so it can take over")
                return code
            remaining = (wake - clock()).total_seconds()
            if remaining <= 0:
                break
            sleep(min(remaining, POLL_SECONDS))


def _hand_over(hooks: Hooks) -> bool:
    if hooks.hand_over():
        return True
    log.error("Could not start the next run; pending orders wait until a run is started some other way")
    return False


def command_persister(command: Optional[str], timeout: float = 600.0) -> Callable[[], str]:
    """Wrap a shell command (e.g. the workflow's commit-and-push script) as a persist hook."""
    if not command:
        return _saved
    args = shlex.split(command)

    def persist() -> str:
        try:
            code = subprocess.run(args, timeout=timeout).returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("Saving state failed: %s", type(exc).__name__)
            return FAILED
        if code == 0:
            return SAVED
        return RESTART if code == RESTART_EXIT_CODE else FAILED

    return persist


class GitHubActions:
    """The two GitHub API calls the loop needs when it runs inside a workflow."""

    def __init__(
        self,
        repository: str,
        token: str,
        run_id: int,
        workflow_file: str,
        ref: str,
        api_url: str = "https://api.github.com",
        session: Optional[requests.Session] = None,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base = f"{api_url.rstrip('/')}/repos/{repository}/actions/workflows/{workflow_file}"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "x-daily-automation/1.0",
        }
        self._run_id = run_id
        self._ref = ref
        self._session = session or requests.Session()
        self._timeout = timeout
        self._sleep = sleep

    def another_run_waiting(self) -> bool:
        """True if a run of this workflow started after this one and has not finished.

        Such a run is queued behind this one (the workflow allows one at a time).
        Any failure to ask counts as "no", so an API hiccup never ends a run early.
        """
        try:
            resp = self._session.get(
                f"{self._base}/runs", params={"per_page": 20}, headers=self._headers, timeout=self._timeout
            )
            if resp.status_code != 200:
                return False
            runs: Any = resp.json().get("workflow_runs", [])
        except (requests.RequestException, ValueError, AttributeError):
            return False
        return any(
            isinstance(run, dict)
            and isinstance(run.get("id"), int)
            and run["id"] > self._run_id
            and run.get("status") != "completed"
            for run in (runs if isinstance(runs, list) else [])
        )

    def hand_over(self) -> bool:
        """Start a fresh run of the workflow, marked as a hand-over rather than a manual run."""
        for attempt in (1, 2, 3):
            try:
                resp = self._session.post(
                    f"{self._base}/dispatches",
                    json={"ref": self._ref, "inputs": {"handover": "true"}},
                    headers=self._headers,
                    timeout=self._timeout,
                )
                if resp.status_code == 204:
                    return True
                log.warning("GitHub did not start the next run (HTTP %s)", resp.status_code)
            except requests.RequestException as exc:
                log.warning("GitHub could not be reached to start the next run (%s)", type(exc).__name__)
            if attempt < 3:
                self._sleep(5 * attempt)
        return False


def hooks_from_env(env: Mapping[str, str]) -> Hooks:
    """Hooks for wherever the bot is running: a GitHub workflow, or a plain machine."""
    persist = command_persister(env.get("PERSIST_COMMAND"))
    needed = ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_REF_NAME", "WORKFLOW_FILE")
    if not all(env.get(name) for name in needed) or not env["GITHUB_RUN_ID"].isdigit():
        return Hooks(persist=persist)
    actions = GitHubActions(
        env["GITHUB_REPOSITORY"],
        env["GITHUB_TOKEN"],
        int(env["GITHUB_RUN_ID"]),
        env["WORKFLOW_FILE"],
        env["GITHUB_REF_NAME"],
        api_url=env.get("GITHUB_API_URL") or "https://api.github.com",
    )
    return Hooks(persist=persist, another_run_waiting=actions.another_run_waiting, hand_over=actions.hand_over)

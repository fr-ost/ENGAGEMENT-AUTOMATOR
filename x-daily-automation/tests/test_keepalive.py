import json
import sys
from datetime import datetime, timedelta, timezone

import requests

from src import keepalive
from src.keepalive import FAILED, RESTART, SAVED, GitHubActions, Hooks, command_persister, hooks_from_env, run_loop

START = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
API = "https://api.github.com/repos/someone/bot/actions/workflows/daily.yml"


class Surroundings:
    """Everything run_loop touches, faked: time only moves when the loop sleeps."""

    def __init__(self, wakes, codes=None, saves=None, waiting_after=None, hand_over_ok=True):
        self.now = START
        self.wakes = list(wakes)            # what next_wake returns after each cycle
        self.codes = list(codes or [])      # exit code of each cycle (default 0)
        self.saves = list(saves or [])      # result of each save (default SAVED)
        self.waiting_after = waiting_after  # another run starts waiting at this time
        self.hand_over_ok = hand_over_ok
        self.cycles, self.sleeps, self.handovers, self.polls = [], [], 0, 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)

    def cycle(self, first):
        self.cycles.append((self.now, first))
        return self.codes.pop(0) if self.codes else 0

    def next_wake(self, now):
        return self.wakes.pop(0) if self.wakes else None

    def persist(self):
        return self.saves.pop(0) if self.saves else SAVED

    def another_run_waiting(self):
        self.polls += 1
        return self.waiting_after is not None and self.now >= self.waiting_after

    def hand_over(self):
        self.handovers += 1
        return self.hand_over_ok

    def run(self, max_minutes=0):
        hooks = Hooks(persist=self.persist, another_run_waiting=self.another_run_waiting, hand_over=self.hand_over)
        return run_loop(self.cycle, self.next_wake, hooks, max_minutes, clock=self.clock, sleep=self.sleep)


def at(**delta):
    return START + timedelta(**delta)


def test_one_cycle_when_nothing_is_scheduled():
    s = Surroundings(wakes=[None], codes=[1])
    assert s.run() == 1                       # the cycle's own exit code
    assert s.cycles == [(START, True)] and s.sleeps == [] and s.handovers == 0


def test_stays_alive_and_runs_each_cycle_exactly_when_due():
    s = Surroundings(wakes=[at(minutes=52), at(hours=2), None])
    assert s.run() == 0
    assert s.cycles == [(START, True), (at(minutes=52), False), (at(hours=2), False)]
    assert max(s.sleeps) <= keepalive.POLL_SECONDS          # sleeps in short stretches...
    assert s.polls >= len(s.sleeps)                         # ...checking for a waiting run in between
    assert s.handovers == 0


def test_cycles_never_start_less_than_five_minutes_apart():
    s = Surroundings(wakes=[START, START, None])            # "something is due right now", twice
    s.run()
    assert [when for when, _ in s.cycles] == [START, at(minutes=5), at(minutes=10)]


def test_hands_over_to_a_fresh_run_before_the_time_limit():
    s = Surroundings(wakes=[at(hours=2), at(hours=4), at(hours=6)])
    assert s.run(max_minutes=320) == 0
    assert [when for when, _ in s.cycles] == [START, at(hours=2), at(hours=4)]   # 6h is past 5h20m
    assert s.handovers == 1
    assert s.now == at(hours=4)                             # it did not sleep toward a time it cannot reach


def test_failed_hand_over_is_an_error():
    s = Surroundings(wakes=[at(hours=6)], hand_over_ok=False)
    assert s.run(max_minutes=320) == 1


def test_steps_aside_as_soon_as_another_run_is_waiting():
    s = Surroundings(wakes=[at(hours=1)], waiting_after=at(minutes=7))
    assert s.run() == 0
    assert len(s.cycles) == 1 and s.handovers == 0          # the waiting run takes over; nothing to start
    assert at(minutes=7) <= s.now <= at(minutes=10)


def test_stops_when_state_cannot_be_saved():
    s = Surroundings(wakes=[at(hours=1)], saves=[FAILED])
    assert s.run() == 1
    assert len(s.cycles) == 1 and s.sleeps == [] and s.handovers == 0


def test_code_change_hands_over_only_if_something_is_still_scheduled():
    s = Surroundings(wakes=[at(hours=1)], saves=[RESTART])
    assert s.run() == 0 and s.handovers == 1 and len(s.cycles) == 1
    idle = Surroundings(wakes=[None], saves=[RESTART])
    assert idle.run() == 0 and idle.handovers == 0


def test_state_or_config_error_ends_the_run_immediately():
    s = Surroundings(wakes=[at(hours=1)], codes=[3])
    assert s.run() == 3
    assert s.sleeps == [] and s.polls == 0


# -- GitHub API ---------------------------------------------------------------


def actions(run_id=500, **kwargs):
    return GitHubActions("someone/bot", "ghs_secret", run_id, "daily.yml", "main", sleep=lambda _: None, **kwargs)


def runs(*pairs):
    return {"workflow_runs": [{"id": run_id, "status": status} for run_id, status in pairs]}


def test_newer_unfinished_run_counts_as_waiting(mocked):
    mocked.get(f"{API}/runs", json=runs((501, "pending"), (500, "in_progress"), (499, "completed")))
    assert actions().another_run_waiting() is True
    request = mocked.calls[0].request
    assert request.headers["Authorization"] == "Bearer ghs_secret"
    assert request.params == {"per_page": "20"}


def test_own_run_older_runs_and_finished_runs_do_not_count(mocked):
    mocked.get(f"{API}/runs", json=runs((501, "completed"), (500, "in_progress"), (400, "in_progress"), (399, "queued")))
    assert actions().another_run_waiting() is False


def test_api_trouble_never_ends_a_run_early(mocked):
    mocked.get(f"{API}/runs", status=500)
    assert actions().another_run_waiting() is False
    mocked.replace("GET", f"{API}/runs", body=requests.ConnectionError())
    assert actions().another_run_waiting() is False
    mocked.replace("GET", f"{API}/runs", body="not json")
    assert actions().another_run_waiting() is False
    mocked.replace("GET", f"{API}/runs", json=["unexpected"])
    assert actions().another_run_waiting() is False


def test_hand_over_starts_a_run_marked_as_hand_over(mocked):
    mocked.post(f"{API}/dispatches", status=204)
    assert actions().hand_over() is True
    assert json.loads(mocked.calls[0].request.body) == {"ref": "main", "inputs": {"handover": "true"}}


def test_hand_over_retries_then_reports_failure(mocked):
    mocked.post(f"{API}/dispatches", status=403, json={"message": "Resource not accessible by integration"})
    assert actions().hand_over() is False
    assert len(mocked.calls) == 3
    mocked.replace("POST", f"{API}/dispatches", body=requests.Timeout())
    assert actions().hand_over() is False


def test_hooks_outside_github_do_nothing():
    hooks = hooks_from_env({})
    assert hooks.persist() == SAVED and hooks.another_run_waiting() is False and hooks.hand_over() is False


def test_hooks_inside_a_workflow_use_the_github_api(mocked):
    env = {
        "GITHUB_TOKEN": "ghs_secret", "GITHUB_REPOSITORY": "someone/bot", "GITHUB_RUN_ID": "500",
        "GITHUB_REF_NAME": "main", "WORKFLOW_FILE": "daily.yml",
    }
    mocked.get(f"{API}/runs", json=runs((777, "queued")))
    mocked.post(f"{API}/dispatches", status=204)
    hooks = hooks_from_env(env)
    assert hooks.another_run_waiting() is True and hooks.hand_over() is True
    assert hooks_from_env({**env, "GITHUB_RUN_ID": ""}).another_run_waiting() is False


def test_save_command_exit_codes():
    def saver(code):
        return command_persister(f'"{sys.executable}" -c "import sys; sys.exit({code})"')

    assert saver(0)() == SAVED
    assert saver(10)() == RESTART
    assert saver(1)() == FAILED
    assert command_persister("/no/such/program --flag")() == FAILED
    assert command_persister(None)() == SAVED and command_persister("")() == SAVED

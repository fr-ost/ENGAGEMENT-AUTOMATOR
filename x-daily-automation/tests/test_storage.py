import json
import os
import time

import pytest

from src import storage
from src.storage import (
    BotState,
    LockError,
    PersistenceError,
    StateCorruptError,
    StateStore,
    atomic_write_json,
    empty_campaigns,
    empty_posts,
    empty_state,
    record_run,
)


def sample_state():
    state, posts, campaigns = empty_state(), empty_posts(), empty_campaigns()
    state["last_successful_scan_at"] = "2026-10-25T17:00:00Z"
    posts["posts"]["111"] = {
        "post_id": "111",
        "post_url": "https://x.com/u/status/111",
        "created_at": "2026-10-25T10:00:00.000Z",
        "discovered_at": "2026-10-25T17:00:00Z",
        "status": "PLANNED",
    }
    campaigns["plans"]["111"] = {"post_id": "111", "targets": {"impressions": 1000, "likes": 30}, "authorized_campaigns": []}
    return BotState(state, posts, campaigns)


def test_first_run_with_no_files(tmp_path):
    loaded = StateStore(tmp_path / "data").load()
    assert loaded.first_run
    assert loaded.state["last_successful_scan_at"] is None
    assert loaded.posts["posts"] == {}


def test_round_trip(tmp_path):
    store = StateStore(tmp_path)
    store.save(sample_state())
    loaded = store.load()
    assert not loaded.first_run
    assert loaded.state["last_successful_scan_at"] == "2026-10-25T17:00:00Z"
    assert "111" in loaded.posts["posts"]


def test_atomic_write_keeps_original_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    atomic_write_json(target, {"v": 1})

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", broken_replace)
    with pytest.raises(PersistenceError, match="disk full"):
        atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # temp file cleaned up


def test_atomic_write_rejects_unserializable(tmp_path):
    with pytest.raises(PersistenceError):
        atomic_write_json(tmp_path / "x.json", {"bad": object()})
    assert not (tmp_path / "x.json").exists()


def test_save_writes_checkpoint_file_last(tmp_path, monkeypatch):
    order = []
    real = storage.atomic_write_json
    monkeypatch.setattr(storage, "atomic_write_json", lambda p, d: (order.append(p.name), real(p, d)))
    StateStore(tmp_path).save(sample_state())
    assert order[-1] == "state.json"


def test_failed_posts_write_does_not_advance_checkpoint(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    store.save(sample_state())
    newer = sample_state()
    newer.state["last_successful_scan_at"] = "2026-10-26T17:00:00Z"
    real = storage.atomic_write_json

    def fail_on_posts(path, data):
        if path.name == "posts.json":
            raise PersistenceError("simulated")
        real(path, data)

    monkeypatch.setattr(storage, "atomic_write_json", fail_on_posts)
    with pytest.raises(PersistenceError):
        store.save(newer)
    monkeypatch.undo()
    assert store.load().state["last_successful_scan_at"] == "2026-10-25T17:00:00Z"


def test_corrupt_json_is_never_silently_reset(tmp_path):
    store = StateStore(tmp_path)
    store.save(sample_state())
    (tmp_path / "posts.json").write_text("{ not json")
    with pytest.raises(StateCorruptError, match="posts.json"):
        store.load()
    assert (tmp_path / "posts.json").read_text() == "{ not json"  # untouched


def test_missing_file_among_others_is_an_error(tmp_path):
    store = StateStore(tmp_path)
    store.save(sample_state())
    os.remove(tmp_path / "campaigns.json")
    with pytest.raises(StateCorruptError, match="campaigns.json is missing"):
        store.load()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(schema_version=99),
        lambda s: s.update(last_successful_scan_at="yesterday"),
        lambda s: s.update(account=[]),
        lambda s: s.update(run_history={}),
    ],
)
def test_invalid_state_schema(tmp_path, mutate):
    store = StateStore(tmp_path)
    store.save(sample_state())
    data = json.loads((tmp_path / "state.json").read_text())
    mutate(data)
    (tmp_path / "state.json").write_text(json.dumps(data))
    with pytest.raises(StateCorruptError):
        store.load()


def test_post_id_mismatch_is_invalid(tmp_path):
    store = StateStore(tmp_path)
    store.save(sample_state())
    data = json.loads((tmp_path / "posts.json").read_text())
    data["posts"]["111"]["post_id"] = "222"
    (tmp_path / "posts.json").write_text(json.dumps(data))
    with pytest.raises(StateCorruptError, match="post_id matches"):
        store.load()


def test_reinit_quarantines_only_the_bad_file(tmp_path):
    store = StateStore(tmp_path)
    store.save(sample_state())
    (tmp_path / "posts.json").write_text("garbage")
    loaded = store.load(allow_reinit=True)
    assert loaded.posts["posts"] == {}
    assert loaded.state["last_successful_scan_at"] == "2026-10-25T17:00:00Z"  # intact
    assert loaded.recovery_notes and "posts.json" in loaded.recovery_notes[0]
    quarantined = list((tmp_path / "quarantine").iterdir())
    assert len(quarantined) == 1 and quarantined[0].read_text() == "garbage"


def test_lock_blocks_overlapping_runs(tmp_path):
    store = StateStore(tmp_path)
    with store.lock():
        with pytest.raises(LockError):
            with store.lock():
                pass
    assert not (tmp_path / ".run.lock").exists()
    with store.lock():  # released, can be taken again
        pass


def test_stale_lock_is_replaced(tmp_path):
    store = StateStore(tmp_path)
    lock = tmp_path / ".run.lock"
    lock.write_text("{}")
    old = time.time() - 10_000
    os.utime(lock, (old, old))
    with store.lock(stale_after=60):
        assert lock.exists()
    assert not lock.exists()


def test_lock_released_on_exception(tmp_path):
    store = StateStore(tmp_path)
    with pytest.raises(RuntimeError):
        with store.lock():
            raise RuntimeError("boom")
    assert not (tmp_path / ".run.lock").exists()


def test_run_history_is_bounded():
    state = empty_state()
    for i in range(storage.RUN_HISTORY_LIMIT + 5):
        record_run(state, {"n": i})
    assert len(state["run_history"]) == storage.RUN_HISTORY_LIMIT
    assert state["last_run"] == {"n": storage.RUN_HISTORY_LIMIT + 4}

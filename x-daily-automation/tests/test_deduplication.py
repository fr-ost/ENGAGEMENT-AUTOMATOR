from src.deduplication import deduplicate
from src.scanner import Post


def post(pid):
    return Post(pid, "t", "2026-10-26T10:00:00Z", f"https://x.com/u/status/{pid}", "original")


def test_known_ids_are_skipped():
    new, dups = deduplicate([post("1"), post("2"), post("3")], {"2"})
    assert [p.post_id for p in new] == ["1", "3"]
    assert dups == 1


def test_duplicates_within_batch_are_skipped():
    new, dups = deduplicate([post("1"), post("1"), post("2")], [])
    assert [p.post_id for p in new] == ["1", "2"]
    assert dups == 1


def test_everything_known_yields_nothing():
    new, dups = deduplicate([post("1"), post("2")], {"1", "2"})
    assert new == [] and dups == 2

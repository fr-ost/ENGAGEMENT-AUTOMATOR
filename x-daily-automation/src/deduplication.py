"""Deduplication by immutable X post ID."""

from __future__ import annotations

from typing import Iterable, Protocol, TypeVar


class HasPostId(Protocol):
    post_id: str


T = TypeVar("T", bound=HasPostId)


def deduplicate(posts: Iterable[T], known_ids: Iterable[str]) -> tuple[list[T], int]:
    """Return (new posts in input order, number of duplicates skipped).

    A post is a duplicate if its ID is already known or appeared earlier in the
    same batch (overlapping scan windows and pagination can repeat posts).
    """
    seen = set(known_ids)
    new: list[T] = []
    duplicates = 0
    for post in posts:
        if post.post_id in seen:
            duplicates += 1
            continue
        seen.add(post.post_id)
        new.append(post)
    return new, duplicates

"""Split a view total into uneven, natural-looking orders spread over time."""

from __future__ import annotations

import math
import random


def plan_quantities(
    total: int,
    min_qty: int,
    max_qty: int,
    rng: random.Random,
    parts_range: tuple[int, int] = (10, 12),
) -> list[int]:
    """Uneven positive integers that sum exactly to `total`, each within [min_qty, max_qty]."""
    min_qty = max(1, min_qty)
    if total < min_qty:
        raise ValueError(f"{total:,} is below the service minimum of {min_qty:,}")
    parts = rng.randint(*parts_range)
    parts = max(1, min(parts, total // min_qty))
    parts = max(parts, math.ceil(total / max_qty))
    if parts * min_qty > total:
        raise ValueError(f"{total:,} cannot be split within the service limits {min_qty:,}-{max_qty:,}")

    weights = [rng.uniform(0.6, 1.4) for _ in range(parts)]
    spare = total - parts * min_qty
    scale = spare / sum(weights)
    quantities = [min_qty + int(w * scale) for w in weights]
    for _ in range(total - sum(quantities)):  # rounding leftovers
        quantities[rng.randrange(parts)] += 1

    # Push anything above the maximum onto the smallest parts.
    while max(quantities) > max_qty:
        high = quantities.index(max(quantities))
        low = quantities.index(min(quantities))
        move = min(quantities[high] - max_qty, max_qty - quantities[low])
        quantities[high] -= move
        quantities[low] += move

    # Nudge round numbers (500, 420) so the orders do not look machine-made.
    for i in range(parts):
        j = (i + 1) % parts
        if i == j or quantities[i] % 10:
            continue
        delta = rng.randint(1, 7)
        if quantities[i] + delta <= max_qty and quantities[j] - delta >= min_qty:
            quantities[i] += delta
            quantities[j] -= delta
    return quantities


def plan_times(count: int, start: float, spread_seconds: float, rng: random.Random) -> list[float]:
    """First order at `start`; the rest roughly evenly spaced over the window, with jitter."""
    if count <= 1:
        return [start] * count
    slot = spread_seconds / count
    return [start] + [start + slot * (i + rng.uniform(-0.3, 0.3)) for i in range(1, count)]

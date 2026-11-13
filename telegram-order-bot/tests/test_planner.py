import random

import pytest

from orderbot.planner import plan_quantities, plan_times


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("total,low,high", [(5000, 100, 100000), (1000, 100, 5000), (12345, 50, 2000), (777, 10, 100)])
def test_quantities_sum_and_respect_limits(seed, total, low, high):
    parts = plan_quantities(total, low, high, random.Random(seed))
    assert sum(parts) == total
    assert all(low <= p <= high for p in parts)


def test_5000_views_gives_10_to_12_uneven_orders():
    parts = plan_quantities(5000, 100, 100000, random.Random(1))
    assert 10 <= len(parts) <= 12
    assert len(set(parts)) > 1
    assert sum(1 for p in parts if p % 100 == 0) <= 1


def test_small_total_uses_fewer_orders():
    assert len(plan_quantities(350, 100, 10000, random.Random(3))) == 3
    assert plan_quantities(100, 100, 10000, random.Random(3)) == [100]


def test_below_minimum_is_refused():
    with pytest.raises(ValueError):
        plan_quantities(50, 100, 10000, random.Random(0))


def test_times_start_now_and_stay_in_window():
    times = plan_times(11, 1000.0, 12 * 3600, random.Random(5))
    assert times[0] == 1000.0
    assert times == sorted(times)
    assert times[-1] < 1000.0 + 12 * 3600
    assert plan_times(1, 5.0, 3600, random.Random(0)) == [5.0]

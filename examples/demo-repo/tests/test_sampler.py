import pytest

from widget import Sampler, score


def test_score_penalises_cost():
    assert score({"value": 1.0, "cost": 0.0}) > score({"value": 1.0, "cost": 1.0})


def test_empty_pool_rejected():
    with pytest.raises(ValueError):
        Sampler([])


def test_exploit_picks_best():
    s = Sampler([{"value": 1.0}, {"value": 5.0}], explore=0.0, seed=1)
    assert s.pick()["value"] == 5.0


def test_pool_exhausts():
    s = Sampler([{"value": 1.0}], explore=0.0, seed=1)
    s.pick()
    with pytest.raises(RuntimeError):
        s.pick()

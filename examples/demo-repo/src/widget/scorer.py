"""Scoring for candidate items."""


def score(item: dict) -> float:
    """A candidate's scalar score. Higher is better."""
    return float(item.get("value", 0.0)) - 0.1 * float(item.get("cost", 0.0))

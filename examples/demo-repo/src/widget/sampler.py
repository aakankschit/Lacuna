"""Selection from a candidate pool with a fixed exploration split."""
import random

from .scorer import score


class Sampler:
    """Picks items from a pool: a fixed fraction at random, the rest by score.

    The split is a constant. There is no batching and no prior carried between runs; both are
    recorded as gaps in the README.
    """

    def __init__(self, pool: list[dict], explore: float = 0.2, seed: int | None = None):
        if not pool:
            raise ValueError("pool is empty")
        if not 0.0 <= explore <= 1.0:
            raise ValueError(f"explore must be in [0, 1], got {explore}")
        self.pool = list(pool)
        self.explore = explore
        self.rng = random.Random(seed)
        self.seen: list[int] = []

    def pick(self) -> dict:
        """One item. Random with probability `explore`, otherwise the best unseen by score."""
        unseen = [i for i in range(len(self.pool)) if i not in self.seen]
        if not unseen:
            raise RuntimeError("pool exhausted")
        if self.rng.random() < self.explore:
            i = self.rng.choice(unseen)
        else:
            i = max(unseen, key=lambda j: score(self.pool[j]))
        self.seen.append(i)
        return self.pool[i]

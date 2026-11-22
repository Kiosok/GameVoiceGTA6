"""Small timing helpers shared by capture diagnostics and the Tk preview."""
from collections import deque
import math


class FrameRate:
    """Count delivered frames, not the configured camera frame rate."""
    def __init__(self, window=2.0):
        self.window = window
        self.stamps = deque()

    def _prune(self, now):
        while self.stamps and self.stamps[0] < now - self.window:
            self.stamps.popleft()

    def record(self, now):
        self.stamps.append(now)
        self._prune(now)

    def value(self, now):
        self._prune(now)
        if len(self.stamps) < 2 or now - self.stamps[-1] > .5:
            return 0.0
        elapsed = self.stamps[-1] - self.stamps[0]
        return (len(self.stamps) - 1) / elapsed if elapsed > 0 else 0.0


class DeadlineTicker:
    """Schedule from deadlines, so rendering time does not slow each next tick."""
    def __init__(self, fps, now):
        self.period = 1.0 / fps
        self.deadline = now

    def next_delay_ms(self, now):
        self.deadline += self.period
        if self.deadline <= now:
            # Skip missed ticks instead of busy-looping through stale work.
            self.deadline += (math.floor((now - self.deadline) / self.period) + 1) * self.period
        return max(1, math.ceil((self.deadline - now) * 1000))

from collections import deque
from typing import List, Optional


class LoadBalancer:
    STRATEGIES = ("round_robin", "latency_aware")

    def __init__(self, models: List[str], strategy: str = "round_robin"):
        if strategy not in self.STRATEGIES:
            raise ValueError(f"Unknown strategy {strategy!r}. Choose from {self.STRATEGIES}")
        self.models = list(models)
        self.strategy = strategy
        self._queue: deque = deque(models)
        self._latencies: dict = {m: 0.0 for m in models}
        self._alpha = 0.2  # EWMA smoothing factor

    def next(self) -> str:
        if not self.models:
            raise ValueError("No models registered")
        if self.strategy == "latency_aware":
            return self._latency_aware()
        return self._round_robin()

    def next_excluding(self, exclude: str) -> Optional[str]:
        available = [m for m in self.models if m != exclude]
        if not available:
            return None
        if self.strategy == "latency_aware":
            return min(available, key=lambda m: self._latencies[m])
        # Round-robin among available: find next in queue that isn't excluded
        for _ in range(len(self._queue)):
            candidate = self._queue[0]
            self._queue.rotate(-1)
            if candidate != exclude:
                return candidate
        return available[0]

    def _round_robin(self) -> str:
        model = self._queue[0]
        self._queue.rotate(-1)
        return model

    def _latency_aware(self) -> str:
        return min(self.models, key=lambda m: self._latencies[m])

    def record_latency(self, model: str, latency_seconds: float) -> None:
        if model not in self._latencies:
            self._latencies[model] = latency_seconds
            return
        current = self._latencies[model]
        self._latencies[model] = self._alpha * latency_seconds + (1 - self._alpha) * current

    def get_latencies(self) -> dict:
        return dict(self._latencies)

    def add_model(self, model: str) -> None:
        if model not in self.models:
            self.models.append(model)
            self._queue.append(model)
            self._latencies[model] = 0.0

    def remove_model(self, model: str) -> None:
        if model in self.models:
            self.models.remove(model)
            try:
                self._queue.remove(model)
            except ValueError:
                pass
            self._latencies.pop(model, None)

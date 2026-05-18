import pytest
from load_balancer import LoadBalancer


class TestRoundRobin:
    def test_cycles_through_all_models(self):
        lb = LoadBalancer(["a", "b", "c"], strategy="round_robin")
        results = [lb.next() for _ in range(6)]
        assert results == ["a", "b", "c", "a", "b", "c"]

    def test_single_model_always_returned(self):
        lb = LoadBalancer(["only"], strategy="round_robin")
        for _ in range(5):
            assert lb.next() == "only"

    def test_next_excluding_returns_other(self):
        lb = LoadBalancer(["a", "b", "c"], strategy="round_robin")
        result = lb.next_excluding("a")
        assert result in ("b", "c")

    def test_next_excluding_all_returns_none(self):
        lb = LoadBalancer(["solo"], strategy="round_robin")
        assert lb.next_excluding("solo") is None

    def test_next_excluding_two_models(self):
        lb = LoadBalancer(["x", "y"], strategy="round_robin")
        result = lb.next_excluding("x")
        assert result == "y"

    def test_raises_on_empty_models(self):
        lb = LoadBalancer(["a"])
        lb.remove_model("a")
        with pytest.raises(ValueError):
            lb.next()


class TestLatencyAware:
    def test_picks_lowest_latency(self):
        lb = LoadBalancer(["fast", "slow", "mid"], strategy="latency_aware")
        lb._latencies = {"fast": 0.01, "slow": 1.0, "mid": 0.1}
        assert lb.next() == "fast"

    def test_ties_return_first_alphabetically(self):
        lb = LoadBalancer(["a", "b"], strategy="latency_aware")
        lb._latencies = {"a": 0.0, "b": 0.0}
        assert lb.next() == "a"

    def test_next_excluding_picks_second_lowest(self):
        lb = LoadBalancer(["a", "b", "c"], strategy="latency_aware")
        lb._latencies = {"a": 0.01, "b": 0.5, "c": 0.1}
        result = lb.next_excluding("a")
        assert result == "c"


class TestLatencyRecording:
    def test_ewma_converges(self):
        lb = LoadBalancer(["m"], strategy="latency_aware")
        for _ in range(50):
            lb.record_latency("m", 1.0)
        assert abs(lb._latencies["m"] - 1.0) < 0.01

    def test_ewma_responds_to_change(self):
        lb = LoadBalancer(["m"], strategy="latency_aware")
        lb._latencies["m"] = 0.0
        lb.record_latency("m", 1.0)
        assert lb._latencies["m"] > 0

    def test_record_latency_unknown_model_sets_value(self):
        lb = LoadBalancer(["a"])
        lb.record_latency("unknown", 0.5)
        assert lb._latencies["unknown"] == 0.5


class TestModelManagement:
    def test_add_model(self):
        lb = LoadBalancer(["a"])
        lb.add_model("b")
        assert "b" in lb.models

    def test_add_model_idempotent(self):
        lb = LoadBalancer(["a"])
        lb.add_model("a")
        assert lb.models.count("a") == 1

    def test_remove_model(self):
        lb = LoadBalancer(["a", "b"])
        lb.remove_model("a")
        assert "a" not in lb.models

    def test_remove_unknown_model_safe(self):
        lb = LoadBalancer(["a"])
        lb.remove_model("nonexistent")  # no exception

    def test_get_latencies_returns_copy(self):
        lb = LoadBalancer(["a", "b"])
        lat = lb.get_latencies()
        lat["a"] = 999
        assert lb._latencies["a"] == 0.0


class TestValidation:
    def test_invalid_strategy_raises(self):
        with pytest.raises(ValueError, match="Unknown strategy"):
            LoadBalancer(["a"], strategy="magic")

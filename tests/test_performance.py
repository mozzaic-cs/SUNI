"""
Performance and stability tests with metrics output.

Measures:
  - Latency: p50 / p95 / p99 for status, auth/me, and chat endpoints
  - Concurrency: 10 simultaneous requests against /api/status
  - Memory: RSS growth over 30 sequential chat requests
  - Throughput: requests/second for lightweight endpoints

All metrics are printed as a formatted table at the end of the test run.
Tests PASS when p95 latency is within acceptable limits (see THRESHOLDS).
"""
from __future__ import annotations
import statistics
import time
import concurrent.futures
import threading
import pytest

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# ── Acceptable latency limits (seconds) ──────────────────────────────────────
THRESHOLDS = {
    "status_p95":   0.5,    # /api/status should be fast even cold
    "auth_me_p95":  0.3,    # /api/auth/me = JWT decode only
    "chat_p95":    10.0,    # chat includes streaming sleep delays (~0.018s/word)
}

SAMPLE_N   = 20  # number of requests per latency test
CONCURRENT = 10  # workers for concurrency test


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(data: list[float], pct: int) -> float:
    sorted_d = sorted(data)
    idx = max(0, int(len(sorted_d) * pct / 100) - 1)
    return sorted_d[idx]


def _measure(fn, n: int) -> dict:
    """Run fn() n times and return latency statistics."""
    samples: list[float] = []
    errors = 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            errors += 1
        finally:
            samples.append(time.perf_counter() - t0)
    return {
        "n":      n,
        "errors": errors,
        "mean":   statistics.mean(samples),
        "median": statistics.median(samples),
        "p95":    _percentile(samples, 95),
        "p99":    _percentile(samples, 99),
        "min":    min(samples),
        "max":    max(samples),
    }


_print_lock = threading.Lock()
_results: dict[str, dict] = {}


def _record(name: str, metrics: dict) -> None:
    _results[name] = metrics
    with _print_lock:
        print(
            f"\n[PERF] {name:30s}  "
            f"p50={metrics['median']*1000:.1f}ms  "
            f"p95={metrics['p95']*1000:.1f}ms  "
            f"p99={metrics['p99']*1000:.1f}ms  "
            f"mean={metrics['mean']*1000:.1f}ms  "
            f"err={metrics['errors']}/{metrics['n']}"
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLatency:

    def test_status_endpoint_latency(self, client, admin_headers):
        def call():
            r = client.get("/api/status", headers=admin_headers)
            assert r.status_code == 200

        m = _measure(call, SAMPLE_N)
        _record("GET /api/status", m)

        assert m["errors"] == 0, f"{m['errors']} errors during status latency test"
        assert m["p95"] < THRESHOLDS["status_p95"], (
            f"Status p95={m['p95']*1000:.1f}ms exceeds {THRESHOLDS['status_p95']*1000:.0f}ms"
        )

    def test_auth_me_latency(self, client, admin_headers):
        def call():
            r = client.get("/api/auth/me", headers=admin_headers)
            assert r.status_code == 200

        m = _measure(call, SAMPLE_N)
        _record("GET /api/auth/me", m)

        assert m["errors"] == 0
        assert m["p95"] < THRESHOLDS["auth_me_p95"], (
            f"auth/me p95={m['p95']*1000:.1f}ms exceeds {THRESHOLDS['auth_me_p95']*1000:.0f}ms"
        )

    def test_chat_endpoint_latency(self, client, std_headers):
        def call():
            r = client.post("/api/chat",
                            json={"message": "hi"},
                            headers=std_headers)
            assert r.status_code == 200

        # Fewer samples — chat is slow due to streaming delays
        m = _measure(call, min(SAMPLE_N, 5))
        _record("POST /api/chat (mock)", m)

        assert m["errors"] == 0
        assert m["p95"] < THRESHOLDS["chat_p95"], (
            f"chat p95={m['p95']*1000:.1f}ms exceeds {THRESHOLDS['chat_p95']*1000:.0f}ms"
        )

    def test_config_read_latency(self, client, admin_headers):
        def call():
            r = client.get("/api/config", headers=admin_headers)
            assert r.status_code == 200

        m = _measure(call, SAMPLE_N)
        _record("GET /api/config", m)
        assert m["errors"] == 0


class TestConcurrency:

    def test_concurrent_status_requests(self, client, admin_headers):
        """All CONCURRENT workers hit /api/status simultaneously."""
        errors: list[str] = []
        latencies: list[float] = []
        lock = threading.Lock()

        def worker():
            t0 = time.perf_counter()
            try:
                r = client.get("/api/status", headers=admin_headers)
                if r.status_code != 200:
                    with lock:
                        errors.append(f"status {r.status_code}")
            except Exception as e:
                with lock:
                    errors.append(str(e))
            finally:
                with lock:
                    latencies.append(time.perf_counter() - t0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
            futures = [pool.submit(worker) for _ in range(CONCURRENT)]
            concurrent.futures.wait(futures)

        throughput = CONCURRENT / max(latencies)
        m = {
            "n":        CONCURRENT,
            "errors":   len(errors),
            "mean":     statistics.mean(latencies),
            "median":   statistics.median(latencies),
            "p95":      _percentile(latencies, 95),
            "p99":      _percentile(latencies, 99),
            "min":      min(latencies),
            "max":      max(latencies),
        }
        _record(f"GET /api/status (x{CONCURRENT} concurrent)", m)
        print(f"\n[PERF] Throughput (concurrent): {throughput:.1f} req/s  errors: {errors}")

        assert len(errors) == 0, f"Concurrent errors: {errors}"
        # All requests should complete in reasonable time (not serialised)
        assert m["max"] < 5.0, f"Max concurrent latency {m['max']*1000:.0f}ms is too high"

    def test_concurrent_auth_requests(self, client, admin_headers):
        """10 simultaneous /api/auth/me — verifies no race on JWT decode."""
        results = []
        lock = threading.Lock()

        def worker():
            r = client.get("/api/auth/me", headers=admin_headers)
            with lock:
                results.append(r.status_code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENT) as pool:
            futures = [pool.submit(worker) for _ in range(CONCURRENT)]
            concurrent.futures.wait(futures)

        assert all(s == 200 for s in results), f"Some auth requests failed: {results}"


class TestMemoryStability:

    @pytest.mark.skipif(not _HAS_PSUTIL, reason="psutil not installed")
    def test_memory_does_not_grow_unboundedly(self, client, std_headers):
        """Send 20 chat requests and verify RSS growth stays below 50 MB."""
        import psutil
        import os

        proc = psutil.Process(os.getpid())
        baseline_mb = proc.memory_info().rss / 1_048_576

        for _ in range(20):
            client.post("/api/chat",
                        json={"message": "hello"},
                        headers=std_headers)

        final_mb = proc.memory_info().rss / 1_048_576
        growth_mb = final_mb - baseline_mb

        print(f"\n[PERF] Memory: baseline={baseline_mb:.1f}MB  "
              f"final={final_mb:.1f}MB  growth={growth_mb:.1f}MB")

        assert growth_mb < 50, (
            f"Memory grew {growth_mb:.1f}MB over 20 chat requests (limit: 50MB)"
        )

    @pytest.mark.skipif(not _HAS_PSUTIL, reason="psutil not installed")
    def test_memory_baseline(self, client, admin_headers):
        """Just log the current process RSS for the test report."""
        import psutil
        import os

        proc = psutil.Process(os.getpid())
        rss_mb = proc.memory_info().rss / 1_048_576
        print(f"\n[PERF] Process RSS at test time: {rss_mb:.1f} MB")
        assert rss_mb < 2048, f"Baseline RSS {rss_mb:.1f}MB seems too high"


class TestThroughput:

    def test_status_throughput(self, client, admin_headers):
        """Measure raw request throughput (sequential) for /api/status."""
        n = 30
        t0 = time.perf_counter()
        for _ in range(n):
            r = client.get("/api/status", headers=admin_headers)
            assert r.status_code == 200
        elapsed = time.perf_counter() - t0
        rps = n / elapsed
        print(f"\n[PERF] /api/status throughput: {rps:.1f} req/s over {n} requests")
        # At minimum we expect > 10 req/s even in a slow test environment
        assert rps > 10, f"Throughput {rps:.1f} req/s is below 10 req/s minimum"

    def test_auth_me_throughput(self, client, admin_headers):
        n = 30
        t0 = time.perf_counter()
        for _ in range(n):
            r = client.get("/api/auth/me", headers=admin_headers)
            assert r.status_code == 200
        elapsed = time.perf_counter() - t0
        rps = n / elapsed
        print(f"\n[PERF] /api/auth/me throughput: {rps:.1f} req/s over {n} requests")
        assert rps > 10

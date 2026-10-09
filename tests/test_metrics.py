"""Search metrics in both modes: EMF lines (default) and the `put_metric_data` fallback."""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import boto3
import pytest
from moto import mock_aws

from kb import metrics
from kb.logging import setup_logging
from kb.metrics import ApiMetricsSender, Sample, emit_search_metrics, metrics_mode


class StubCloudWatch:
    """Records `put_metric_data` calls; can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def put_metric_data(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("cloudwatch is down")
        self.calls.append(kwargs)


@pytest.fixture(autouse=True)
def _clean_metrics_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(metrics.MODE_ENV, raising=False)
    monkeypatch.setattr(metrics, "_warned_mode", False)
    previous = metrics.set_sender(None)
    yield
    leftover = metrics.set_sender(previous)
    if leftover is not None:
        leftover.stop()


def sample(mode: str = "semantic", latency: float = 10.0, level: int = 2, ts: float = 1_700_000_000.0) -> Sample:
    return Sample(ts, mode, latency, level, 1 if mode == "keyword" else 0)


def emf_line(mode: str = "keyword") -> dict[str, Any]:
    buf = io.StringIO()
    setup_logging(buf)
    emit_search_metrics(mode, "high", 12.0)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


# --- mode switch --------------------------------------------------------------------------


def test_default_mode_is_emf_and_unset_or_empty_means_emf(monkeypatch: pytest.MonkeyPatch) -> None:
    assert metrics_mode() == "emf"
    monkeypatch.setenv(metrics.MODE_ENV, "")
    assert metrics_mode() == "emf"
    monkeypatch.setenv(metrics.MODE_ENV, " API ")
    assert metrics_mode() == "api"


def test_unknown_mode_falls_back_to_emf_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(metrics.MODE_ENV, "statsd")
    with caplog.at_level(logging.WARNING, logger="kb.metrics"):
        assert metrics_mode() == "emf"
        assert metrics_mode() == "emf"
    assert len([r for r in caplog.records if "unknown" in r.getMessage()]) == 1


def test_emf_mode_writes_an_emf_line_and_never_touches_the_sender() -> None:
    line = emf_line("keyword")
    assert "_aws" in line and line["Mode"] == "keyword" and line["KeywordSearches"] == 1
    assert metrics._sender is None


def test_api_mode_logs_no_emf_block_and_buffers_a_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(metrics.MODE_ENV, "api")
    stub = StubCloudWatch()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    metrics.set_sender(sender)
    line = emf_line("keyword")
    assert "_aws" not in line and line["msg"] == "search metrics" and line["Mode"] == "keyword"
    assert sender.pending() == 1 and stub.calls == []


# --- the API sender -----------------------------------------------------------------------


def test_flush_sends_one_datum_per_metric_and_mode_with_values_and_counts() -> None:
    stub = StubCloudWatch()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    for latency in (10.0, 10.0, 30.0):
        sender.submit(sample("semantic", latency))
    sender.submit(sample("keyword", 5.0, level=1))
    assert sender.flush() == 4 and sender.pending() == 0
    (call,) = stub.calls
    assert call["Namespace"] == "KbMcp"
    by_key = {(d["MetricName"], d["Dimensions"][0]["Value"]): d for d in call["MetricData"]}
    assert set(by_key) == {
        (name, mode)
        for name in ("LatencyMs", "ConfidenceLevel", "KeywordSearches")
        for mode in ("semantic", "keyword")
    }
    latency = by_key[("LatencyMs", "semantic")]
    assert latency["Values"] == [10.0, 30.0] and latency["Counts"] == [2.0, 1.0]
    assert latency["Unit"] == "Milliseconds"
    assert by_key[("KeywordSearches", "keyword")]["Values"] == [1.0]
    assert by_key[("KeywordSearches", "semantic")]["Values"] == [0.0]
    assert by_key[("ConfidenceLevel", "keyword")]["Values"] == [1.0]
    assert all(d["Dimensions"] == [{"Name": "Mode", "Value": d["Dimensions"][0]["Value"]}] for d in call["MetricData"])


def test_api_metrics_use_the_same_names_units_and_dimension_as_the_emf_line() -> None:
    emf = emf_line("keyword")["_aws"]["CloudWatchMetrics"][0]
    stub = StubCloudWatch()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    sender.submit(sample("keyword", 5.0))
    sender.flush()
    data = stub.calls[0]["MetricData"]
    assert stub.calls[0]["Namespace"] == emf["Namespace"]
    assert {d["MetricName"]: d["Unit"] for d in data} == {m["Name"]: m["Unit"] for m in emf["Metrics"]}
    assert [[dim["Name"] for dim in d["Dimensions"]] for d in data][0] == emf["Dimensions"][0]


def test_no_datum_exceeds_150_values_and_samples_go_in_batches() -> None:
    stub = StubCloudWatch()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    for n in range(350):
        sender.submit(sample("semantic", float(n)))  # 350 distinct latencies, one minute
    assert sender.flush() == 350
    assert len(stub.calls) == 4  # 100 samples per call
    for call in stub.calls:
        assert len(call["MetricData"]) <= 1000
        assert all(len(d["Values"]) <= 150 and len(d["Values"]) == len(d["Counts"]) for d in call["MetricData"])
    assert sum(sum(d["Counts"]) for c in stub.calls for d in c["MetricData"] if d["MetricName"] == "LatencyMs") == 350
    assert metrics._build_data([sample(latency=float(n)) for n in range(400)])
    assert all(len(d["Values"]) <= 150 for d in metrics._build_data([sample(latency=float(n)) for n in range(400)]))


def test_failed_flush_keeps_the_samples_for_the_next_one() -> None:
    stub = StubCloudWatch()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    sender.submit(sample("keyword", 5.0))
    sender.submit(sample("semantic", 7.0))
    stub.fail = True
    assert sender.flush() == 0 and sender.pending() == 2
    stub.fail = False
    assert sender.flush() == 2 and sender.pending() == 0
    assert sender.flush() == 0 and len(stub.calls) == 1


def test_partial_failure_requeues_only_the_unsent_samples() -> None:
    class FailsSecond(StubCloudWatch):
        attempts = 0

        def put_metric_data(self, **kwargs: Any) -> None:
            self.attempts += 1
            if self.attempts == 2:
                raise RuntimeError("throttled")
            super().put_metric_data(**kwargs)

    stub = FailsSecond()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    for n in range(250):
        sender.submit(sample(latency=float(n)))
    assert sender.flush() == 100 and sender.pending() == 150
    assert sender.flush() == 150 and sender.pending() == 0


def test_buffer_is_capped_when_cloudwatch_stays_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "MAX_BUFFERED", 50)
    stub = StubCloudWatch()
    stub.fail = True
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    for n in range(80):
        sender.submit(sample(latency=float(n)))
    assert sender.pending() == 50
    sender.flush()
    assert sender.pending() == 50


def test_a_client_that_cannot_be_built_does_not_raise() -> None:
    def broken() -> Any:
        raise RuntimeError("no credentials")

    sender = ApiMetricsSender(broken, interval=3600)
    sender.submit(sample())
    assert sender.flush() == 0 and sender.pending() == 1


def test_background_thread_flushes_on_its_interval_and_stop_flushes_the_rest() -> None:
    stub = StubCloudWatch()
    sent = threading.Event()
    original = stub.put_metric_data

    def put(**kwargs: Any) -> None:
        original(**kwargs)
        sent.set()

    stub.put_metric_data = put  # type: ignore[method-assign]
    sender = ApiMetricsSender(lambda: stub, interval=0.05)
    sender.submit(sample())
    assert sent.wait(timeout=5), "the flush thread never sent the sample"
    assert sender.pending() == 0
    sender.submit(sample("keyword", 3.0))
    sender.stop()
    assert sender.pending() == 0
    assert sum(len(c["MetricData"]) for c in stub.calls) >= 6
    assert sender._thread is not None and not sender._thread.is_alive()


def test_submit_does_not_wait_for_the_network() -> None:
    gate = threading.Event()

    class Slow(StubCloudWatch):
        def put_metric_data(self, **kwargs: Any) -> None:
            gate.wait(timeout=10)

    sender = ApiMetricsSender(lambda: Slow(), interval=0.01)
    sender.submit(sample())
    time.sleep(0.1)  # the thread is now stuck inside put_metric_data
    start = time.perf_counter()
    for _ in range(100):
        sender.submit(sample())
    assert time.perf_counter() - start < 0.5
    gate.set()
    sender.stop()


# --- against moto's CloudWatch ------------------------------------------------------------


def test_api_mode_end_to_end_with_moto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(metrics.MODE_ENV, "api")
    with mock_aws():
        sender = ApiMetricsSender(interval=3600)  # default factory: a real boto3 client under moto
        metrics.set_sender(sender)
        for latency in (100.0, 200.0, 300.0):
            emit_search_metrics("semantic", "high", latency)
        emit_search_metrics("keyword", "low", 8.0)
        emit_search_metrics("keyword", "medium", 9.0)
        assert sender.flush() == 5

        cw = boto3.client("cloudwatch", region_name="us-east-1")
        listed = cw.list_metrics(Namespace="KbMcp")["Metrics"]
        assert {m["MetricName"] for m in listed} == {"LatencyMs", "ConfidenceLevel", "KeywordSearches"}
        assert {tuple((d["Name"], d["Value"]) for d in m["Dimensions"]) for m in listed} == {
            (("Mode", "semantic"),),
            (("Mode", "keyword"),),
        }

        def stat(name: str, mode: str, statistic: str) -> float:
            now = datetime.now(timezone.utc)
            out = cw.get_metric_statistics(
                Namespace="KbMcp",
                MetricName=name,
                Dimensions=[{"Name": "Mode", "Value": mode}],
                StartTime=now - timedelta(hours=1),
                EndTime=now + timedelta(hours=1),
                Period=3600 * 3,
                Statistics=[statistic],
            )
            return sum(point[statistic] for point in out["Datapoints"])

        # What the kb-keyword-mode alarm reads: Sum of KeywordSearches with Mode=keyword.
        assert stat("KeywordSearches", "keyword", "Sum") == 2
        assert stat("KeywordSearches", "semantic", "Sum") == 0
        assert stat("LatencyMs", "semantic", "SampleCount") == 3
        assert stat("LatencyMs", "semantic", "Sum") == 600
        assert stat("LatencyMs", "semantic", "Maximum") == 300
        assert stat("ConfidenceLevel", "keyword", "Sum") == 1  # low (0) + medium (1)


def test_a_real_search_in_api_mode_reaches_the_sender(
    monkeypatch: pytest.MonkeyPatch, open_app: Any
) -> None:
    monkeypatch.setenv(metrics.MODE_ENV, "api")
    stub = StubCloudWatch()
    sender = ApiMetricsSender(lambda: stub, interval=3600)
    metrics.set_sender(sender)
    with open_app(models=False) as client:
        resp = client.get("/api/search", params={"q": "glacier lifecycle"})
        assert resp.status_code == 200
    assert sender.pending() == 1
    sender.flush()
    names = {d["MetricName"] for d in stub.calls[0]["MetricData"]}
    modes = {d["Dimensions"][0]["Value"] for d in stub.calls[0]["MetricData"]}
    assert names == {"LatencyMs", "ConfidenceLevel", "KeywordSearches"} and modes == {"keyword"}

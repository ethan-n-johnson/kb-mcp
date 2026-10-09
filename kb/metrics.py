"""Search metrics for CloudWatch: Embedded Metric Format lines, or `put_metric_data`.

`KB_METRICS_MODE` picks how one search becomes metrics:

- `emf` (default): log one EMF line per search. The container's `awslogs` log
  driver ships it (with `awslogs-format=json/emf`) and CloudWatch Logs extracts
  the metrics from the `_aws` block.
- `api`: no EMF block; the sample goes into a buffer that a background thread
  sends with boto3 `put_metric_data` every 30 seconds. This is the fallback if
  EMF extraction does not work (phase 15, task 3). Flip it by setting the
  variable on the container; no code change.

Both modes publish the same names under the same dimensions, so the alarms in
`infra/app.yaml` do not care which one is active. Namespace `KbMcp`, dimension
`Mode` (`semantic` or `keyword`), metrics `LatencyMs`, `ConfidenceLevel` and
`KeywordSearches`.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("kb.metrics")

NAMESPACE = "KbMcp"
CONFIDENCE_LEVELS = {"high": 2, "medium": 1, "low": 0}
METRIC_UNITS = {"LatencyMs": "Milliseconds", "ConfidenceLevel": "None", "KeywordSearches": "Count"}

MODE_ENV = "KB_METRICS_MODE"
FLUSH_SECONDS = 30.0
MAX_BUFFERED = 5000  # samples; the oldest are dropped if CloudWatch stays unreachable
_VALUES_PER_DATUM = 150  # CloudWatch limit for the Values array of one datum
# 100 samples make at most 300 data per call (3 metrics), well under the limit of 1000.
_SAMPLES_PER_CALL = 100

_warned_mode = False


def metrics_mode() -> str:
    """Return `emf` or `api` from `KB_METRICS_MODE` (default and fallback: `emf`).

    An unknown value logs one warning and means `emf`.
    """
    global _warned_mode
    raw = (os.environ.get(MODE_ENV) or "emf").strip().lower()
    if raw in ("emf", "api"):
        return raw
    if not _warned_mode:
        _warned_mode = True
        log.warning("unknown %s value, using emf", MODE_ENV, extra={"value": raw[:20]})
    return "emf"


@dataclass(frozen=True)
class Sample:
    """One search, ready to be sent as metrics."""

    timestamp: float
    mode: str
    latency_ms: float
    confidence_level: int
    keyword: int

    def values(self) -> dict[str, float]:
        """Metric name to value, the same numbers the EMF line carries."""
        return {
            "LatencyMs": self.latency_ms,
            "ConfidenceLevel": float(self.confidence_level),
            "KeywordSearches": float(self.keyword),
        }


class ApiMetricsSender:
    """Buffers samples and sends them with `put_metric_data` from a daemon thread.

    `submit` is cheap and never blocks on the network. `flush` sends what is
    buffered (batched per metric, mode and minute, up to 150 values per datum)
    and puts the unsent part back if CloudWatch fails, so a short outage loses
    nothing (up to `MAX_BUFFERED` samples). The thread starts on the first `submit`.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any] | None = None,
        *,
        interval: float = FLUSH_SECONDS,
        namespace: str = NAMESPACE,
    ) -> None:
        self._factory = client_factory or _default_client
        self._interval = interval
        self._namespace = namespace
        self._lock = threading.Lock()
        self._buffer: list[Sample] = []
        self._client: Any | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def submit(self, sample: Sample) -> None:
        """Buffer one sample and make sure the flush thread runs."""
        with self._lock:
            self._buffer.append(sample)
            if len(self._buffer) > MAX_BUFFERED:
                del self._buffer[: len(self._buffer) - MAX_BUFFERED]
            if self._thread is None and not self._stop.is_set():
                self._thread = threading.Thread(target=self._run, name="kb-metrics", daemon=True)
                self._thread.start()

    def pending(self) -> int:
        """Number of buffered samples not yet sent."""
        with self._lock:
            return len(self._buffer)

    def flush(self) -> int:
        """Send everything buffered now; return the number of samples sent.

        Never raises. On an error the samples not yet accepted go back into the buffer.
        """
        with self._lock:
            samples, self._buffer = self._buffer, []
        if not samples:
            return 0
        sent = 0
        try:
            if self._client is None:
                self._client = self._factory()
            while sent < len(samples):
                batch = samples[sent : sent + _SAMPLES_PER_CALL]
                self._client.put_metric_data(Namespace=self._namespace, MetricData=_build_data(batch))
                sent += len(batch)
        except Exception:  # boto raises many types; all mean "try again later"
            log.warning("put_metric_data failed, will retry", exc_info=True)
            with self._lock:  # only what was not accepted goes back
                self._buffer = (samples[sent:] + self._buffer)[-MAX_BUFFERED:]
            return sent
        return sent

    def stop(self) -> None:
        """Stop the thread and send what is left."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.flush()
            except Exception:  # never let the thread die
                log.exception("metrics flush crashed")


def _default_client() -> Any:
    import boto3  # lazy: emf mode and the test suite never need it

    return boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION") or "us-east-1")


def _build_data(samples: list[Sample]) -> list[dict[str, Any]]:
    """Group samples into `MetricData` with `Values` and `Counts` per metric, mode and minute."""
    groups: dict[tuple[str, str, int], dict[float, int]] = {}
    for sample in samples:
        minute = int(sample.timestamp // 60) * 60
        for name, value in sample.values().items():
            counts = groups.setdefault((name, sample.mode, minute), {})
            counts[value] = counts.get(value, 0) + 1
    data: list[dict[str, Any]] = []
    for (name, mode, minute), counts in sorted(groups.items()):
        items = sorted(counts.items())
        for start in range(0, len(items), _VALUES_PER_DATUM):
            part = items[start : start + _VALUES_PER_DATUM]
            data.append(
                {
                    "MetricName": name,
                    "Dimensions": [{"Name": "Mode", "Value": mode}],
                    "Timestamp": minute,
                    "Values": [v for v, _ in part],
                    "Counts": [float(c) for _, c in part],
                    "Unit": METRIC_UNITS[name],
                }
            )
    return data


_sender: ApiMetricsSender | None = None
_sender_lock = threading.Lock()


def get_sender() -> ApiMetricsSender:
    """Return the process-wide sender used in `api` mode, creating it on first use."""
    global _sender
    with _sender_lock:
        if _sender is None:
            _sender = ApiMetricsSender()
            atexit.register(_sender.stop)
        return _sender


def set_sender(sender: ApiMetricsSender | None) -> ApiMetricsSender | None:
    """Swap the process-wide sender (tests); returns the previous one."""
    global _sender
    with _sender_lock:
        previous, _sender = _sender, sender
        return previous


def emit_search_metrics(mode: str, confidence: str, latency_ms: float) -> None:
    """Publish the metrics of one search; never raises.

    Namespace `KbMcp`, dimension `Mode`, metrics `LatencyMs` (Milliseconds),
    `ConfidenceLevel` (high=2, medium=1, low=0) and `KeywordSearches`
    (1 for a keyword search, else 0). With `KB_METRICS_MODE=emf` (default) this
    logs one EMF line; with `api` it buffers a sample for `put_metric_data` and
    logs a plain line (no `_aws` block, so nothing is counted twice).
    """
    try:
        level = CONFIDENCE_LEVELS.get(confidence, 0)
        fields = {
            "Mode": mode,
            "LatencyMs": round(latency_ms, 2),
            "ConfidenceLevel": level,
            "KeywordSearches": 1 if mode == "keyword" else 0,
        }
        if metrics_mode() == "api":
            get_sender().submit(
                Sample(
                    timestamp=time.time(),
                    mode=mode,
                    latency_ms=float(fields["LatencyMs"]),
                    confidence_level=level,
                    keyword=int(fields["KeywordSearches"]),
                )
            )
            log.info("search metrics", extra=fields)
            return
        log.info(
            "search metrics",
            extra={
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": NAMESPACE,
                            "Dimensions": [["Mode"]],
                            "Metrics": [
                                {"Name": name, "Unit": unit} for name, unit in METRIC_UNITS.items()
                            ],
                        }
                    ],
                },
                **fields,
            },
        )
    except Exception:  # metrics must never break a search
        log.debug("could not emit metrics", exc_info=True)

"""A server's Prometheus metrics (/metrics): fetched, parsed into series
with their labels, and compared between two snapshots.

Other traffic moves a server's counters too: the directors' and the
servers' own tests, and the other suites' requests. So a test should
expect a counter to have risen by at least, or at most, what it did,
not by exactly that, unless the series is the test's alone.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from . import web

# A series' labels, sorted by name.
Labels = tuple[tuple[str, str], ...]

# A sample's line: the metric, its labels if any, and its value, perhaps
# with a timestamp after it.
_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?$")
_LABEL = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*,?')
_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}


def _labels(text: str) -> Labels:
    """The labels in text, the part of a sample between its braces."""
    found = {
        m.group(1): re.sub(r"\\.", lambda e: _ESCAPES.get(e.group(0), e.group(0)), m.group(2))
        for m in _LABEL.finditer(text)
    }
    return tuple(sorted(found.items()))


@dataclass(frozen=True)
class Snapshot:
    """A server's metrics at one moment: each series' value, by its
    metric's name and its labels."""

    samples: Mapping[tuple[str, Labels], float]

    def series(self, name: str, /, **labels: str) -> dict[Labels, float]:
        """Every series of metric name that has labels (and perhaps
        others), by all its labels."""
        wanted = set(labels.items())
        return {
            have: value
            for (metric, have), value in self.samples.items()
            if metric == name and wanted <= set(have)
        }

    def value(self, name: str, /, **labels: str) -> float:
        """The sum of series(), 0 if there is none: e.g. of
        pelican_http_requests_total with method="GET", whatever its
        other labels."""
        return sum(self.series(name, **labels).values())

    def has(self, name: str) -> bool:
        """Whether metric name has any series, which a metric with labels
        has only once something has happened."""
        return any(metric == name for metric, _ in self.samples)

    def has_prefix(self, prefix: str) -> bool:
        """Whether any metric's name begins with prefix."""
        return any(metric.startswith(prefix) for metric, _ in self.samples)


# No metrics: what a server that did not answer has, where that is not
# worth telling apart.
EMPTY = Snapshot({})


def parse(text: str) -> Snapshot:
    """The series in text, a Prometheus text exposition. Lines it cannot
    read, and comments, are passed over."""
    samples: dict[tuple[str, Labels], float] = {}
    for line in text.splitlines():
        found = _SAMPLE.match(line.strip())
        if found is None or line.startswith("#"):
            continue
        try:
            value = float(found.group(3))
        except ValueError:
            continue
        samples[(found.group(1), _labels(found.group(2) or ""))] = value
    return Snapshot(samples)


def fetch(url: str, token: Optional[str] = None) -> Optional[Snapshot]:
    """The metrics of the server at web URL url (e.g. a common.Origin's
    web_url), presenting token, as a server needs when
    Monitoring.MetricAuthorization is on (a token of the server's with
    scope monitoring.scrape); None if it does not answer 200."""
    answer = web.request("GET", f"{url}/metrics", token=token)
    if answer.status != 200:
        return None
    return parse(answer.body.decode(errors="replace"))


def delta(before: Snapshot, after: Snapshot, name: str, /, **labels: str) -> float:
    """How much the sum of metric name's series with labels rose from
    before to after (see Snapshot.value())."""
    return after.value(name, **labels) - before.value(name, **labels)

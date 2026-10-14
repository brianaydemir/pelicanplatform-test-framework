"""What the metadata recorder keeps of the origins' deliveries to it.

The recorder (framework/metadata-server/recorder.py) keeps them in
framework/var/data/metadata/ (CAPTURES), by the ID it gives each, which
is when the delivery arrived (time.time_ns(), by the recorder's clock,
which is the dev container's too: both are containers on one host):

  index.tsv        a row per delivery, `<id> <status> <type> <path>
                   <event id>`, with the status the recorder answered
  <id>.json        the request, and the status and body returned
  <id>.event.json  the event as sent
  <id>.blob        the uploader's opaque blob, if one came along
"""

import json
from typing import Any

from . import common

CAPTURES = "data/metadata"


def index() -> list[list[str]]:
    """Return the rows of the recorder's index.tsv, split into columns.

    Returns:
        Each row's columns, <id> <status> <type> <path> <event id>; none
        if there is no index yet.
    """
    try:
        with open(f"{CAPTURES}/index.tsv", encoding="utf-8") as f:
            return [line.rstrip("\n").split("\t") for line in f]
    except FileNotFoundError:
        return []


def since(after: int) -> list[list[str]]:
    """Return index()'s rows for the deliveries that arrived after after.

    Args:
        after: A time.time_ns().
    """
    return [
        row for row in index() if len(row) >= 5 and row[0].isdigit() and int(row[0]) > after
    ]


def deliveries(after: int, path: str = "", event_id: str = "") -> list[list[str]]:
    """Return since(after)'s rows for object path, or for event event_id."""
    return [
        row
        for row in since(after)
        if (path and row[3] == path) or (event_id and row[4] == event_id)
    ]


def event(capture: str, part: str) -> dict[str, Any]:
    """Return a part of the captured event, or {} if it has none.

    Args:
        capture: The delivery's ID.
        part: The part's key, e.g. "object".
    """
    with open(f"{CAPTURES}/{capture}.event.json", encoding="utf-8") as f:
        whole = common.as_object(json.load(f)) or {}
    return common.as_object(whole.get(part)) or {}


def last_delivery(row: list[str]) -> str:
    """Return how the delivery that index() row records went.

    Returns:
        The status and answer in its <id>.json, or, if that can't be
        read, the row's.
    """
    try:
        with open(f"{CAPTURES}/{row[0]}.json", encoding="utf-8") as f:
            capture = json.load(f)
        got, answer = capture.get("status"), str(capture.get("response", "")).rstrip()
    except (OSError, ValueError, AttributeError):
        got, answer = (row[1] if len(row) > 1 else "?"), "(no record of the answer)"
    return f"last delivery got HTTP {got}: {answer}"


def delivered(path: str, code: str) -> bool:
    """Return whether the recorder answered a delivery for path with code.

    Args:
        path: The object's path.
        code: An HTTP status, e.g. "422".
    """
    return any(row[1:2] == [code] and row[3:4] == [path] for row in index())

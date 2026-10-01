"""Server reads the pages share, cached per browser session in `state.work(ss)`.

The cache is browser-only and never saved. Actions that change a dataset (cleaning,
domain, contract) drop its entries with `invalidate`, on the held work dict.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any

from frontend import state
from frontend.api_client import APIError

KINDS = ("record", "profile", "clean", "detect", "proposal")


def cached(ss: MutableMapping[str, Any], kind: str, dataset_id: str | None,
           fetch: Callable[[], dict]) -> dict | APIError:
    """The cached reply, or fetch it. Errors are returned, never cached."""
    cache = state.work(ss)["cache"]
    key = (kind, dataset_id)
    if key not in cache:
        try:
            cache[key] = fetch()
        except APIError as error:
            return error
    return cache[key]


def invalidate(held_work: dict, dataset_id: str, kinds=KINDS) -> None:
    for kind in kinds:
        held_work["cache"].pop((kind, dataset_id), None)


def label(ss, api, dataset_id: str) -> str:
    record = cached(ss, "record", dataset_id, lambda: api.get_dataset(dataset_id))
    if isinstance(record, APIError):
        return f"{dataset_id} (not available: {record.code})"
    return f"{record['name']} · {dataset_id}"

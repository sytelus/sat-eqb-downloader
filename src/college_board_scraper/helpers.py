from __future__ import annotations

from enum import Enum
from typing import Iterator, Sequence, TypeVar


class ScraperAmount(Enum):
    ALL = "all"
    RANDOM = "random"


T = TypeVar("T")


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    for index in range(0, len(items), size):
        yield items[index : index + size]

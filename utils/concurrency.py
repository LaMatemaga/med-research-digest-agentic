import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def gather_bounded(
    func: Callable[[T], Awaitable[R]], items: Iterable[T], limit: int
) -> list[R | BaseException]:
    """Like asyncio.gather(*(func(i) for i in items), return_exceptions=True), but with
    at most `limit` calls in flight. Results keep input order. The coroutine for each
    item is only created once a slot is free, so nothing starts early."""
    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(item: T) -> R:
        async with semaphore:
            return await func(item)

    return await asyncio.gather(*(run(item) for item in items), return_exceptions=True)

"""Run synchronous database operations without blocking the caller's event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any


class DatabaseWorker:
    """One owner thread for a decision's connections and transactions.

    Cancellation waits for the issued operation to finish before returning
    control to transaction cleanup. A cancelled SQL call must not keep writing
    in the background while another thread rolls back or closes its connection.
    """

    async def __aenter__(self) -> DatabaseWorker:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="baton-secretary")
        return self

    async def call[T](self, function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        future = asyncio.get_running_loop().run_in_executor(self.executor, partial(function, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Repeated cancellation must not abandon an in-flight database operation.
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not future.cancelled():
                future.exception()  # Retrieve a failure; cancellation remains the caller's outcome.
            raise

    async def __aexit__(self, *_: object) -> None:
        await asyncio.to_thread(self.executor.shutdown, wait=True)

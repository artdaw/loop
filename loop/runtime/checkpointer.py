"""The production checkpoint store (agent-stack §4).

`AsyncSqliteSaver` at `data/graph-checkpoints.sqlite` — a specific backend at a
specific path, not "some checkpointer". The choice is deliberate: SQLite is
correct for the single-instance local deployment this spec targets, and a
future multi-worker deployment must explicitly select and test a different
backend rather than silently reusing this file from two processes.

Async, not sync. LangGraph's async checkpoint interface is what lets the
Coordinator's node functions run through `ainvoke` without blocking the event
loop the rest of the service shares — the durable worker dispatcher awaits many
things concurrently, and a synchronous checkpoint write would stall all of them
for the duration of a disk fsync.

The file is secured immediately after creation. Checkpoints can contain private
content: a world-readable checkpoint database would defeat every privacy label
on everything inside it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from loop.runtime.runs import checkpoint_path, secure_checkpoint_file

if TYPE_CHECKING:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)


@asynccontextmanager
async def open_production_checkpointer(data_dir: Path
                                       ) -> AsyncIterator[AsyncSqliteSaver]:  # noqa: F821
    """Open the shared checkpoint database for this deployment.

    One file serves every coordinator run. Restarting the process and opening
    this again against the same path is exactly how a paused run survives a
    restart — the file, not the process, is where execution position lives.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = checkpoint_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        secure_checkpoint_file(path)
        yield saver

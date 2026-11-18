"""Child processes that do not outlive the coroutine awaiting them."""

import asyncio
import contextlib
import os
import signal


async def start(*argv, **kwargs) -> asyncio.subprocess.Process:
    """asyncio.create_subprocess_exec(*argv, **kwargs), leading its own
    process group, so communicate can kill it and every process it starts."""
    return await asyncio.create_subprocess_exec(
        *argv, start_new_session=True, **kwargs
    )


async def communicate(proc: asyncio.subprocess.Process, input=None):
    """proc.communicate(input). If the caller is cancelled, kill proc's
    process group and reap proc before re-raising."""
    try:
        return await proc.communicate(input)
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise

"""Run a command line; when it fails, the error names the command line that
ran, not a retyped copy of it."""

import asyncio
import collections.abc
import pathlib
import shlex


class ProcessError(Exception):
    """A command line that exited with a non-zero status."""

    def __init__(
        self,
        argv: collections.abc.Sequence[str],
        returncode: int,
        stdout: bytes,
        stderr: bytes,
    ):
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self):
        text = f"{shlex.join(self.argv)}: exit {self.returncode}"
        if stderr := self.stderr.decode().strip():
            text = f"{text} - {stderr}"
        return text


async def run(
    argv: collections.abc.Sequence[str],
    cwd: pathlib.Path | None = None,
    stdin: bytes | None = None,
) -> bytes:
    """The stdout of argv, run in cwd with stdin as its input; ProcessError
    when it exits with a non-zero status."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL
        if stdin is None
        else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin)
    if proc.returncode != 0:
        raise ProcessError(
            argv=argv, returncode=proc.returncode, stdout=stdout, stderr=stderr
        )
    return stdout

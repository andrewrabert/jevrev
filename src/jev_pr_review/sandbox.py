"""Run one tool call in a bashkit sandbox whose state persists between calls."""

import argparse
import contextlib
import pathlib
import sqlite3
import sys
import typing

import annotated_types
import bashkit
import pydantic

MAX_BYTES = 256 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    data BLOB NOT NULL
);
"""


class UserError(Exception):
    pass


class FileMissing(UserError):
    pass


# A path inside the sandbox's filesystem.
BashkitPath = typing.Annotated[
    pathlib.PurePosixPath,
    annotated_types.Predicate(pathlib.PurePosixPath.is_absolute),
]

# An existing directory on the host, resolved to an absolute path.
HostPath = typing.Annotated[
    pydantic.DirectoryPath,
    pydantic.AfterValidator(pathlib.Path.resolve),
]


class Mount(pydantic.BaseModel):
    """A host directory mounted in the sandbox, given as HOST_PATH:BASHKIT_PATH[:rw]."""

    host_path: HostPath
    bashkit_path: BashkitPath
    writable: bool = False

    @classmethod
    def parse(cls, value):
        host_path, sep, rest = value.partition(":")
        bashkit_path, _, mode = rest.partition(":")
        if not sep or mode not in ("", "rw"):
            raise ValueError(f"{value}: expected HOST_PATH:BASHKIT_PATH[:rw]")
        return cls(
            host_path=host_path,
            bashkit_path=bashkit_path,
            writable=mode == "rw",
        )


class State:
    """A sandbox's snapshot in a SQLite file."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    @classmethod
    @contextlib.contextmanager
    def open(cls, path: pathlib.Path, lock_timeout: float):
        """Open the state at path, holding its write lock until exit."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(
            sqlite3.connect(path, timeout=lock_timeout, autocommit=True)
        ) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield cls(db)
            finally:
                db.execute("COMMIT")

    def load(self) -> bytes | None:
        """The saved snapshot, or None before the first save."""
        row = self.db.execute("SELECT data FROM snapshot").fetchone()
        return None if row is None else row[0]

    def save(self, data: bytes):
        self.db.execute(
            "INSERT OR REPLACE INTO snapshot (id, data) VALUES (0, ?)", (data,)
        )


class Sandbox:
    def __init__(
        self,
        state: State,
        mounts: list[Mount],
        username: str | None,
        hostname: str | None,
        cwd: str | None,
    ):
        self.state = state
        kwargs = dict(
            username=username,
            hostname=hostname,
            cwd=cwd,
            python=True,
            sqlite=True,
            profile=bashkit.ExecutionProfile.Interactive,
        )
        data = state.load()
        if data is None:
            self.bash = bashkit.Bash(**kwargs)
        else:
            self.bash = bashkit.Bash.from_snapshot(data, **kwargs)
        # Mounts are not in the snapshot: their files stay on the host.
        for mount in mounts:
            self.bash.mount(
                str(mount.bashkit_path),
                bashkit.FileSystem.real(
                    str(mount.host_path), writable=mount.writable
                ),
                read_only=not mount.writable,
            )
        self.fs = self.bash.fs()

    @classmethod
    @contextlib.contextmanager
    def opened(
        cls,
        state: State,
        mounts: list[Mount],
        username: str | None,
        hostname: str | None,
        cwd: str | None,
    ):
        """The sandbox of an open state, saved to it on the way out."""
        sandbox = cls(
            state=state,
            mounts=mounts,
            username=username,
            hostname=hostname,
            cwd=cwd,
        )
        try:
            yield sandbox
        finally:
            sandbox.save()

    def save(self):
        self.state.save(self.bash.snapshot())

    def run_bash(self, command: str) -> str:
        """Run command: its stdout, its stderr, and its exit code unless 0."""
        result = self.bash.execute_sync(command)
        parts = [part for part in (result.stdout, result.stderr) if part]
        if result.exit_code != 0:
            parts.append(f"(exit code {result.exit_code})")
        return "\n".join(parts)

    def read_lines(
        self,
        file_path: BashkitPath,
        offset: int | None = None,
        limit: int | None = None,
        max_file_size: int = 16 * 1024 * 1024,
    ) -> str:
        """Lines of a text file, numbered: limit lines from offset, or all."""
        whole = offset in (None, 1) and limit is None
        # Checked before the read, as bashkit reads only whole files.
        size = self.size(file_path)
        if size > max_file_size:
            raise UserError(
                f"File ({size / 1024 / 1024:.1f}MB) exceeds the largest "
                "file the read tool opens "
                f"({max_file_size / 1024 / 1024:.1f}MB)."
            )
        if whole and size > MAX_BYTES:
            raise UserError(
                f"File content ({size / 1024:.1f}KB) exceeds maximum "
                f"allowed size ({MAX_BYTES // 1024}KB). Use offset and "
                "limit parameters to read specific portions of the file, "
                "or search for specific content instead of reading the "
                "whole file."
            )
        text = self.read_text(file_path)
        if not text:
            raise UserError("File exists but has empty contents.")
        lines = text.split("\n")
        if lines[-1] == "":
            lines.pop()
        start = (offset or 1) - 1
        end = None if limit is None else start + limit
        output = "\n".join(
            f"{start + i + 1:>6}\t{line.removesuffix('\r')}"
            for i, line in enumerate(lines[start:end])
        )
        if (size := len(output.encode())) > MAX_BYTES:
            raise UserError(
                f"The lines read ({size / 1024:.1f}KB) exceed maximum allowed "
                f"size ({MAX_BYTES // 1024}KB). Use a smaller limit."
            )
        return output

    def _get_file_path(self, path: BashkitPath) -> str:
        """path as bashkit takes it, if it names an existing file."""
        name = str(path)
        if not self.fs.exists(name):
            raise FileMissing("File does not exist.")
        if self.fs.stat(name)["file_type"] != "file":
            raise UserError("Illegal operation on a directory")
        return name

    def size(self, path: BashkitPath) -> int:
        return self.fs.stat(self._get_file_path(path))["size"]

    def read_text(self, path: BashkitPath):
        name = self._get_file_path(path)
        try:
            return self.fs.read_file(name).decode()
        except UnicodeDecodeError as e:
            raise UserError(str(e)) from None


def positive_float(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not positive")
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not positive")
    return number


def non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"{value} is negative")
    return number


def mount(value):
    try:
        return Mount.parse(value)
    except pydantic.ValidationError as e:
        problems = "; ".join(
            problem["msg"] for problem in e.errors(include_url=False)
        )
        raise argparse.ArgumentTypeError(f"{value}: {problems}") from e
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def bashkit_path(value):
    path = pathlib.PurePosixPath(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"{value} is not absolute")
    return path


def add_sandbox_arguments(parser):
    group = parser.add_argument_group("sandbox")
    group.add_argument(
        "--state",
        type=pathlib.Path,
        required=True,
        help="SQLite file holding the sandbox's state; created if missing",
    )
    group.add_argument(
        "--mount",
        action="append",
        type=mount,
        help="HOST_PATH:BASHKIT_PATH[:rw], a host directory to mount at "
        "BASHKIT_PATH, read-only unless :rw; repeatable",
    )
    group.add_argument("--username", help="the user name")
    group.add_argument("--hostname", help="the host name")
    group.add_argument("--cwd", help="the initial working directory")
    group.add_argument(
        "--lock-timeout",
        type=positive_float,
        default=3600,
        help="seconds to wait for another run to release the state "
        "(default: 3600)",
    )


@contextlib.contextmanager
def args_sandbox(args):
    """Hold the lock on the sandbox args names, saving it on the way out."""
    with (
        State.open(args.state, args.lock_timeout) as state,
        Sandbox.opened(
            state=state,
            mounts=args.mount or [],
            username=args.username,
            hostname=args.hostname,
            cwd=args.cwd,
        ) as sandbox,
    ):
        yield sandbox


def cmd_bash(args):
    command = sys.stdin.read()
    with args_sandbox(args) as sandbox:
        output = sandbox.run_bash(command)
    sys.stdout.write(output)


def cmd_read(args):
    with args_sandbox(args) as sandbox:
        output = sandbox.read_lines(
            file_path=args.file_path,
            offset=args.offset,
            limit=args.limit,
            max_file_size=args.max_file_size,
        )
    sys.stdout.write(output)


def main():
    parser = argparse.ArgumentParser(
        prog="bashkit-sandbox", description=__doc__
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bash_help = "run the shell command read from stdin"
    bash_parser = subparsers.add_parser(
        "bash", help=bash_help, description=bash_help
    )
    add_sandbox_arguments(bash_parser)

    read_help = "read a text file with numbered lines"
    read_parser = subparsers.add_parser(
        "read", help=read_help, description=read_help
    )
    add_sandbox_arguments(read_parser)
    read_parser.add_argument(
        "--file-path",
        type=bashkit_path,
        required=True,
        help="the file to read",
    )
    read_parser.add_argument(
        "--offset", type=positive_int, help="the first line to read, from 1"
    )
    read_parser.add_argument(
        "--limit", type=non_negative_int, help="the number of lines to read"
    )
    read_parser.add_argument(
        "--max-file-size",
        type=positive_int,
        default=16 * 1024 * 1024,
        help="the largest file, in bytes, to read a range of lines from "
        "(default: 16777216)",
    )

    args = parser.parse_args()
    match args.command:
        case "bash":
            cmd_bash(args)
        case "read":
            cmd_read(args)


def cli():
    try:
        sys.exit(main())
    # bashkit raises RuntimeError for its own errors, such as a read-only
    # filesystem.
    except (UserError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()

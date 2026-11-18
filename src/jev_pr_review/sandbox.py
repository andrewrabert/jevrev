"""Run one tool call in a bashkit sandbox whose state persists between calls."""

import contextlib
import pathlib
import sqlite3
import sys
import typing

import annotated_types
import bashkit
import pydantic
import pydantic_settings

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
        sandbox = cls(state, mounts, username, hostname, cwd)
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


class Tool(pydantic.BaseModel):
    state: pathlib.Path = pydantic.Field(
        description="SQLite file holding the sandbox's state; created if missing"
    )
    mount: list[
        typing.Annotated[Mount, pydantic.BeforeValidator(Mount.parse)]
    ] = pydantic.Field(
        [],
        description="HOST_PATH:BASHKIT_PATH[:rw], a host directory to mount "
        "at BASHKIT_PATH, read-only unless :rw; repeatable",
    )
    username: str | None = pydantic.Field(None, description="the user name")
    hostname: str | None = pydantic.Field(None, description="the host name")
    cwd: str | None = pydantic.Field(
        None, description="the initial working directory"
    )
    lock_timeout: pydantic.PositiveFloat = pydantic.Field(
        3600,
        description="seconds to wait for another run to release the state",
    )

    @contextlib.contextmanager
    def sandbox(self):
        """Hold the lock on the sandbox, saving it on the way out."""
        with (
            State.open(self.state, self.lock_timeout) as state,
            Sandbox.opened(
                state, self.mount, self.username, self.hostname, self.cwd
            ) as sandbox,
        ):
            yield sandbox


class BashTool(Tool):
    """Run the shell command read from stdin."""

    def cli_cmd(self) -> None:
        command = sys.stdin.read()
        with self.sandbox() as sandbox:
            output = sandbox.run_bash(command)
        sys.stdout.write(output)


class ReadTool(Tool):
    """Read a text file with numbered lines."""

    file_path: BashkitPath = pydantic.Field(description="the file to read")
    offset: pydantic.PositiveInt | None = pydantic.Field(
        None, description="the first line to read, from 1"
    )
    limit: pydantic.NonNegativeInt | None = pydantic.Field(
        None, description="the number of lines to read"
    )
    max_file_size: pydantic.PositiveInt = pydantic.Field(
        16 * 1024 * 1024,
        description="the largest file, in bytes, to read a range of lines from",
    )

    def cli_cmd(self) -> None:
        with self.sandbox() as sandbox:
            output = sandbox.read_lines(
                self.file_path, self.offset, self.limit, self.max_file_size
            )
        sys.stdout.write(output)


class Run(pydantic_settings.BaseSettings):
    __doc__ = __doc__

    model_config = pydantic_settings.SettingsConfigDict(
        cli_enforce_required=True,
        cli_implicit_flags="toggle",
        cli_kebab_case=True,
        cli_prog_name="bashkit-sandbox",
    )

    bash: pydantic_settings.CliSubCommand[BashTool]
    read: pydantic_settings.CliSubCommand[ReadTool]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Only the command line sets these.
        return (init_settings,)

    def cli_cmd(self) -> None:
        pydantic_settings.CliApp.run_subcommand(self)


def cli():
    try:
        pydantic_settings.CliApp.run(Run)
    # bashkit raises RuntimeError for its own errors, such as a read-only
    # filesystem.
    except (UserError, pydantic.ValidationError, RuntimeError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()

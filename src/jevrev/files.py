"""The files of one version of a source, wherever they are read from, and the ignore rules over
them."""

import asyncio
import collections
import pathlib
import typing

import pathspec
import pydantic

# A file's path from the top of its source, "/"-separated.
type SourcePath = typing.Annotated[
    str, pydantic.StringConstraints(min_length=1)
]

# A line's number in a file, from 1.
type LineNumber = typing.Annotated[int, pydantic.Field(ge=1)]

# The ignore files of a directory, lowest precedence first.
_IGNORE_FILES = (".gitignore", ".ignore")


class Files(typing.Protocol):
    """The files of one version of a source, by path relative to the source."""

    # Every file path, "/"-separated; never one under .git.
    async def paths(self) -> list[str]: ...

    async def read(self, path: str) -> bytes: ...


def walk(root: pathlib.Path) -> list[str]:
    """Every file under root, relative to it, skipping .git directories and following symlinks.
    Each directory and file is listed once: one in root by its own path, so a symlink into root
    is skipped; one outside by its shallowest path through a symlink, the first in sorted order
    among those as deep. A symlink loop ends."""
    top = root.resolve()
    queue = collections.deque([root])
    found = []
    seen = {top}
    while queue:
        for path in sorted(queue.popleft().iterdir()):
            real = path.resolve()
            if real in seen or (
                path.is_symlink() and real.is_relative_to(top)
            ):
                continue
            seen.add(real)
            if path.is_dir():
                if path.name != ".git":
                    queue.append(path)
            elif path.is_file():
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


class OnDisk:
    """root's files on disk, as walk lists them."""

    def __init__(self, root: pathlib.Path):
        self.root = root.resolve()

    async def paths(self) -> list[str]:
        return await asyncio.to_thread(walk, self.root)

    async def read(self, path: str) -> bytes:
        return await asyncio.to_thread((self.root / path).read_bytes)


class OneFile:
    """One file on disk, by its name in its directory; a symlink is followed."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.name = path.name

    async def paths(self) -> list[str]:
        return [self.name]

    async def read(self, path: str) -> bytes:
        return await asyncio.to_thread(self.path.read_bytes)


def _spec(contents: bytes) -> pathspec.GitIgnoreSpec:
    return pathspec.GitIgnoreSpec.from_lines(
        contents.decode(errors="replace").splitlines()
    )


def _parent_dirs(path: str) -> list[str]:
    """The directories path is under, from the top down, "" being the top."""
    parts = path.split("/")[:-1]
    return [""] + ["/".join(parts[: n + 1]) for n in range(len(parts))]


class Ignore:
    """The ignore rules over a source's files. Paths are matched from the source's repository's
    top: the source's own paths are prefixed with its path from there."""

    def __init__(
        self,
        prefix: str,
        excludes: list[pathspec.GitIgnoreSpec],
        by_dir: dict[str, list[pathspec.GitIgnoreSpec]],
    ):
        self.prefix = prefix
        self.excludes = excludes
        self.by_dir = by_dir

    # .gitignore and .ignore files read through files, and those in above, from the source's top
    # down to each path, plus the contents of each of excludes, lowest precedence first, as
    # gitignore patterns relative to the top.
    # prefix is the source's path from its repository's top, "/"-terminated, or "" at the top;
    # above holds the ignore files of the directories over the source, by directory from the top.
    @classmethod
    async def load(
        cls,
        files: Files,
        excludes: list[bytes],
        prefix: str = "",
        above: typing.Sequence[tuple[str, bytes]] = (),
    ) -> "Ignore":
        by_dir: dict[str, list[pathspec.GitIgnoreSpec]] = {}
        for directory, contents in above:
            by_dir.setdefault(directory, []).append(_spec(contents))
        found = [
            path
            for path in await files.paths()
            if path.rsplit("/", 1)[-1] in _IGNORE_FILES
        ]
        # Within a directory, .ignore is read after .gitignore, so it wins.
        found.sort(
            key=lambda path: (
                path.rsplit("/", 1)[0] if "/" in path else "",
                _IGNORE_FILES.index(path.rsplit("/", 1)[-1]),
            )
        )
        for path in found:
            directory = (
                (prefix + path).rsplit("/", 1)[0]
                if "/" in prefix + path
                else ""
            )
            by_dir.setdefault(directory, []).append(
                _spec(await files.read(path))
            )
        return cls(
            prefix=prefix,
            excludes=[_spec(contents) for contents in excludes],
            by_dir=by_dir,
        )

    def _ignored(self, path: str, is_dir: bool) -> bool:
        """Whether path, from the top, is ignored by the rules that apply to it, ignoring
        whether a directory above it is."""
        name = path + "/" if is_dir else path
        result = None
        for spec in self.excludes:
            if (include := spec.check_file(name).include) is not None:
                result = include
        for directory in _parent_dirs(path):
            relative = name[len(directory) + 1 :] if directory else name
            for spec in self.by_dir.get(directory, ()):
                if (include := spec.check_file(relative).include) is not None:
                    result = include
        return bool(result)

    def matches(self, path: str) -> bool:
        """Whether path, relative to the source, is ignored: it, or a directory above it."""
        path = self.prefix + path
        # git never looks inside an ignored directory, so nothing under one is re-included.
        return any(
            self._ignored(directory, is_dir=True)
            for directory in _parent_dirs(path)[1:]
        ) or self._ignored(path, is_dir=False)

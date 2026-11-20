"""Every git operation jevrev runs, through pygit2. Each returns a model of what libgit2 reports,
with paths and line text decoded from git's bytes."""

import asyncio
import enum
import logging
import os
import pathlib
import typing

import pydantic
import pygit2
import pygit2.enums

import jevrev.files

type GitObjectId = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"^[0-9a-f]{40}$")
]

# A revision as git rev-parse takes it, such as main, HEAD~2 or an object id. Never begins with -, so git
# can't read it as an option, and holds no whitespace or control character.
type GitRevision = typing.Annotated[
    str,
    pydantic.StringConstraints(
        pattern=r"^[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]*$"
    ),
]

# git diff's defaults: its indent heuristic is on unless diff.indentHeuristic turns it off.
_DIFF_FLAGS = pygit2.enums.DiffOption.INDENT_HEURISTIC

_log = logging.getLogger(__name__)


class GitError(Exception):
    pass


def _is_utf8(text):
    """Whether text, decoded with surrogateescape, was valid UTF-8."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def _utf8_path(path: str) -> bool:
    """Whether path, decoded with surrogateescape, was valid UTF-8; one that was not is logged
    as skipped."""
    if _is_utf8(path):
        return True
    _log.info(
        "skipping %s: not a UTF-8 path",
        path.encode(errors="surrogateescape"),
    )
    return False


def _decode(raw: bytes) -> str:
    return raw.decode(errors="surrogateescape")


class HunkLineKind(enum.StrEnum):
    ADDED = "added"
    CONTEXT = "context"
    REMOVED = "removed"


class HunkLine(pydantic.BaseModel):
    """A line of a hunk."""

    # The character a diff writes before the line, as libgit2 gives it in a line's origin.
    prefix: typing.ClassVar[str]

    text: str


class AddedLine(HunkLine):
    prefix = "+"
    kind: typing.Literal[HunkLineKind.ADDED] = HunkLineKind.ADDED
    # The line's number in the file after the change.
    number: jevrev.files.LineNumber


class ContextLine(HunkLine):
    prefix = " "
    kind: typing.Literal[HunkLineKind.CONTEXT] = HunkLineKind.CONTEXT
    # The line's number in the file after the change.
    number: jevrev.files.LineNumber


class RemovedLine(HunkLine):
    """A line of the file before the change that the change removes; it is at the position of
    the hunk's lines it is among."""

    prefix = "-"
    kind: typing.Literal[HunkLineKind.REMOVED] = HunkLineKind.REMOVED


type Line = typing.Annotated[
    AddedLine | ContextLine | RemovedLine, pydantic.Field(discriminator="kind")
]


class Hunk(pydantic.BaseModel):
    source_start: int
    source_length: int
    target_start: int
    target_length: int
    # The function or section git names after the hunk's `@@` line; empty when none.
    section: str
    lines: list[Line]


class ChangedFile(pydantic.BaseModel):
    """A file the diff adds or modifies, by its path."""

    path: jevrev.files.SourcePath
    hunks: list[Hunk]

    @classmethod
    def added(cls, path: str, text: str) -> "ChangedFile":
        """The file at path, of text, added whole."""
        lines = text.splitlines()
        return cls(
            path=path,
            hunks=[
                Hunk(
                    source_start=0,
                    source_length=0,
                    target_start=1,
                    target_length=len(lines),
                    section="",
                    lines=[
                        AddedLine(number=n, text=line)
                        for n, line in enumerate(lines, 1)
                    ],
                )
            ]
            if lines
            else [],
        )


class Diff(pydantic.RootModel[list[ChangedFile]]):
    """The text files a diff adds or modifies. Removed and binary files are left out, and so is a
    file whose path or added and context lines are not UTF-8: it is not text to review. Removed
    lines keep git's bytes, decoded with surrogateescape."""


class _NoNewlineNote(enum.StrEnum):
    """The origin of a note libgit2 adds to a hunk that a side does not end in a newline; not a
    line of the file."""

    BOTH = "="
    NEW = ">"
    OLD = "<"


def _line(line: pygit2.DiffLine) -> Line | None:
    """line as a line of the hunk; None for a note that a side does not end in a newline."""
    text = _decode(line.raw_content).removesuffix("\n")
    match line.origin:
        case AddedLine.prefix:
            return AddedLine(number=line.new_lineno, text=text)
        case ContextLine.prefix:
            return ContextLine(number=line.new_lineno, text=text)
        case RemovedLine.prefix:
            return RemovedLine(text=text)
        case origin if origin in _NoNewlineNote:
            return None
        case origin:
            raise GitError(f"unknown diff line origin {origin!r}")


def _changed_file(patch: pygit2.Patch, path: str) -> ChangedFile | None:
    """The patch's added or modified text file, as path; None when it is removed, binary, not
    UTF-8 or a symlink, whose text is only its target's path."""
    delta = patch.delta
    if (
        delta.status == pygit2.enums.DeltaStatus.DELETED
        or delta.is_binary
        or delta.new_file.mode == pygit2.GIT_FILEMODE_LINK
    ):
        return None
    changed = ChangedFile(
        path=path,
        hunks=[
            Hunk(
                source_start=hunk.old_start,
                source_length=hunk.old_lines,
                target_start=hunk.new_start,
                target_length=hunk.new_lines,
                section=hunk.header.split("@@", 2)[2].strip(),
                lines=[
                    parsed
                    for line in hunk.lines
                    if (parsed := _line(line)) is not None
                ],
            )
            for hunk in patch.hunks
        ],
    )
    if _is_utf8(changed.path) and all(
        _is_utf8(hunk.section) and _is_utf8(line.text)
        for hunk in changed.hunks
        for line in hunk.lines
        if not isinstance(line, RemovedLine)
    ):
        return changed
    return None


class GitRev(pydantic.BaseModel):
    """The contents of the tree at commit."""

    model_config = pydantic.ConfigDict(extra="forbid")

    commit: GitObjectId


class GitDiff(pydantic.BaseModel):
    """The changes from base to head."""

    model_config = pydantic.ConfigDict(extra="forbid")

    base: GitObjectId
    head: GitObjectId


class GitDiffToDisk(pydantic.BaseModel):
    """The changes from base to the files on disk, uncommitted edits included."""

    model_config = pydantic.ConfigDict(extra="forbid")

    base: GitObjectId


class GitDiffSpec(pydantic.BaseModel):
    """A diff in git diff's syntax, before resolution: REV, A..B or A...B, an empty side of the
    last two being HEAD."""

    model_config = pydantic.ConfigDict(extra="forbid")

    base: GitRevision
    # None for the one-revision form, REV: the files on disk.
    head: GitRevision | None
    # A...B: the diff is from merge-base(A, B).
    merge_base: bool

    @classmethod
    def parse(cls, diff: str) -> "GitDiffSpec":
        """diff parsed; ValueError for anything else."""
        for separator, merge_base in (("...", True), ("..", False)):
            if separator in diff:
                base, head = diff.split(separator, 1)
                if separator in head or (not base and not head):
                    raise ValueError(f"{diff!r} is not a diff")
                return cls(
                    base=base or "HEAD",
                    head=head or "HEAD",
                    merge_base=merge_base,
                )
        return cls(base=diff, head=None, merge_base=False)


# File modes of a tree entry that is neither a regular file nor executable.
_NOT_FILES = (pygit2.enums.FileMode.LINK, pygit2.enums.FileMode.COMMIT)


class CommitFiles:
    """root's files at commit, read from root's repository; root may be below the repository's
    top. Symlinks and submodules are left out."""

    def __init__(self, root: pathlib.Path, commit: GitObjectId):
        self.root = root
        self.commit = commit

    def _tree(self) -> tuple["_Repo", pygit2.Tree | None]:
        repo = _Repo(self.root)
        tree = repo.commit(self.commit).tree
        if repo.prefix:
            try:
                tree = tree[repo.prefix.rstrip("/")]
            except KeyError:
                return repo, None
            if not isinstance(tree, pygit2.Tree):
                return repo, None
        return repo, tree

    async def paths(self) -> list[str]:
        def paths():
            _, tree = self._tree()
            if tree is None:
                return []
            found = []
            stack = [("", tree)]
            while stack:
                directory, current = stack.pop()
                for entry in current:
                    path = directory + _decode(entry.raw_name)
                    if isinstance(entry, pygit2.Tree):
                        stack.append((path + "/", entry))
                    elif (
                        isinstance(entry, pygit2.Blob)
                        and entry.filemode not in _NOT_FILES
                        and _utf8_path(path)
                    ):
                        found.append(path)
            return sorted(found)

        return await _run(paths)

    async def read(self, path: str) -> bytes:
        def read():
            _, tree = self._tree()
            if tree is None:
                raise GitError(f"{path} is not in {self.commit}")
            try:
                return tree[path].data
            except KeyError as e:
                raise GitError(f"{path} is not in {self.commit}") from e

        return await _run(read)


class _Repo:
    """A repository opened at root, a directory of its work tree, which paths are relative to."""

    def __init__(self, root: pathlib.Path):
        found = pygit2.discover_repository(str(root))
        if found is None:
            raise GitError(f"{root} is not in a git repository")
        self.repo = pygit2.Repository(found)
        if self.repo.workdir is None:
            raise GitError(f"{root} is in a bare repository")
        self.top = pathlib.Path(self.repo.workdir).resolve()
        relative = root.resolve().relative_to(self.top).as_posix()
        # The root's path from the top, ending in /; empty at the top.
        self.prefix = "" if relative == "." else relative + "/"

    def commit(self, revision: str) -> pygit2.Commit:
        try:
            return self.repo.revparse_single(revision).peel(pygit2.Commit)
        except (KeyError, ValueError, pygit2.GitError) as e:
            raise GitError(f"{revision!r} is not a commit: {e}") from e

    def relative(self, path: str, pathspecs: list[str]) -> str | None:
        """path, from the top, made relative to root; None when outside root or pathspecs."""
        if not path.startswith(self.prefix):
            return None
        path = path.removeprefix(self.prefix)
        if (
            any(
                spec in ("", ".")
                or path == spec
                or path.startswith(spec.rstrip("/") + "/")
                for spec in pathspecs
            )
            or not pathspecs
        ):
            return path
        return None

    def diff(self, diff: pygit2.Diff, pathspecs: list[str]) -> Diff:
        files = []
        for patch in diff:
            path = self.relative(
                _decode(patch.delta.new_file.raw_path), pathspecs
            )
            if path is not None and (changed := _changed_file(patch, path)):
                files.append(changed)
        return Diff(files)

    def untracked(self, pathspecs: list[str], ignored: bool) -> list[str]:
        """The untracked files under root in pathspecs, ignored ones too when ignored, relative
        to root; never a symlink, or a file reached through one."""
        return [
            path
            for path in self._untracked(pathspecs, ignored)
            if (self.top / self.prefix / path).resolve()
            == self.top / self.prefix / path
        ]

    def _untracked(self, pathspecs: list[str], ignored: bool) -> list[str]:
        if ignored:
            # libgit2 lists an ignored directory, not the files under it; the files are every
            # file on disk under root that the index does not hold.
            tracked = {entry.path for entry in self.repo.index}
            return [
                path
                for full in jevrev.files.walk(self.top)
                if full not in tracked
                and (path := self.relative(full, pathspecs)) is not None
                and _utf8_path(path)
            ]
        return [
            path
            for raw, flags in self.repo.status(untracked_files="all").items()
            if flags & pygit2.enums.FileStatus.WT_NEW
            and (path := self.relative(raw, pathspecs)) is not None
            and _utf8_path(path)
        ]

    def ignore_files_above(
        self, commit: GitObjectId | None
    ) -> list[tuple[str, bytes]]:
        """The ignore files of each directory from the top down to root's parent, by
        directory from the top: on disk, or at commit."""
        parts = self.prefix.rstrip("/").split("/") if self.prefix else []
        tree = None if commit is None else self.commit(commit).tree
        found = []
        for n in range(len(parts)):
            directory = "/".join(parts[:n])
            for name in (".gitignore", ".ignore"):
                path = f"{directory}/{name}" if directory else name
                if tree is None:
                    file = self.top / path
                    if file.is_file() and not file.is_symlink():
                        found.append((directory, file.read_bytes()))
                else:
                    try:
                        entry = tree[path]
                    except KeyError:
                        continue
                    if isinstance(entry, pygit2.Blob):
                        found.append((directory, entry.data))
        return found


def _read_if_file(path: pathlib.Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError, NotADirectoryError, IsADirectoryError:
        return None


def _global_excludes() -> list[bytes]:
    try:
        config = pygit2.Config.get_global_config()
    except OSError, pygit2.GitError:
        return _excludes_file(None)
    configured = None
    if "core.excludesFile" in config:
        configured = pathlib.Path(config["core.excludesFile"]).expanduser()
    return _excludes_file(configured)


def _excludes_file(configured: pathlib.Path | None) -> list[bytes]:
    """The contents of core.excludesFile, or $XDG_CONFIG_HOME/git/ignore when it is unset."""
    if configured is None:
        config_home = os.environ.get("XDG_CONFIG_HOME") or str(
            pathlib.Path.home() / ".config"
        )
        configured = pathlib.Path(config_home) / "git" / "ignore"
    contents = _read_if_file(configured)
    return [] if contents is None else [contents]


async def _run[T](operation: typing.Callable[[], T]) -> T:
    """operation, off the event loop, its libgit2 errors as GitError."""
    try:
        return await asyncio.to_thread(operation)
    except pygit2.GitError as e:
        raise GitError(str(e)) from e


class git:
    @staticmethod
    async def top_level(root: pathlib.Path) -> pathlib.Path:
        """The top directory of the work tree root is in."""
        return await _run(lambda: _Repo(root).top)

    @staticmethod
    async def in_work_tree(root: pathlib.Path) -> bool:
        try:
            await git.top_level(root)
        except GitError:
            return False
        return True

    @staticmethod
    async def rev_parse(
        root: pathlib.Path, revisions: list[str]
    ) -> list[GitObjectId]:
        """The commit each of revisions names, in order."""

        def parse():
            repo = _Repo(root)
            return [str(repo.commit(revision).id) for revision in revisions]

        return await _run(parse)

    @staticmethod
    async def merge_base(
        root: pathlib.Path, first: str, second: str
    ) -> GitObjectId:
        def find():
            repo = _Repo(root)
            base = repo.repo.merge_base(
                repo.commit(first).id, repo.commit(second).id
            )
            if base is None:
                raise GitError(f"{first} and {second} have no merge base")
            return str(base)

        return await _run(find)

    @staticmethod
    async def diff_work_tree(
        root: pathlib.Path, revision: str, context: int, pathspecs: list[str]
    ) -> Diff:
        """The diff from revision to the work tree of the files matching pathspecs, with context
        lines around each change and paths relative to root."""

        def diff():
            repo = _Repo(root)
            index = repo.repo.index
            # As git diff REVISION: the tree to the index, then the index to the work tree.
            # pygit2's Tree.diff_to_index ignores its keyword arguments; positional ones apply.
            changes = repo.commit(revision).tree.diff_to_index(
                index, _DIFF_FLAGS, context, 0
            )
            changes.merge(
                index.diff_to_workdir(_DIFF_FLAGS, context_lines=context)
            )
            return repo.diff(changes, pathspecs)

        return await _run(diff)

    @staticmethod
    async def diff_commits(
        root: pathlib.Path,
        base: str,
        head: str,
        context: int,
        inter_hunk_context: int,
    ) -> Diff:
        """The diff from base to head, with context lines around each change, and changes at most
        inter_hunk_context lines apart in one hunk."""

        def diff():
            repo = _Repo(root)
            return repo.diff(
                repo.repo.diff(
                    repo.commit(base),
                    repo.commit(head),
                    flags=_DIFF_FLAGS,
                    context_lines=context,
                    interhunk_lines=inter_hunk_context,
                ),
                [],
            )

        return await _run(diff)

    @staticmethod
    async def untracked_files(
        root: pathlib.Path, pathspecs: list[str], ignored: bool
    ) -> list[str]:
        """The untracked files under root matching pathspecs; with ignored, git-ignored ones
        too."""
        return await _run(lambda: _Repo(root).untracked(pathspecs, ignored))

    @staticmethod
    async def resolve_rev(root: pathlib.Path, rev: GitRevision) -> GitRev:
        """rev resolved in root's repository."""
        return await _run(
            lambda: GitRev(commit=str(_Repo(root).commit(rev).id))
        )

    @staticmethod
    async def resolve_diff(
        root: pathlib.Path, spec: GitDiffSpec
    ) -> GitDiff | GitDiffToDisk:
        """spec resolved in root's repository."""

        def resolve():
            repo = _Repo(root)
            base = repo.commit(spec.base).id
            if spec.head is None:
                return GitDiffToDisk(base=str(base))
            head = repo.commit(spec.head).id
            if spec.merge_base:
                found = repo.repo.merge_base(base, head)
                if found is None:
                    raise GitError(
                        f"{spec.base} and {spec.head} have no merge base"
                    )
                base = found
            return GitDiff(base=str(base), head=str(head))

        return await _run(resolve)

    @staticmethod
    async def excludes(root: pathlib.Path) -> list[bytes]:
        """The contents of core.excludesFile ($XDG_CONFIG_HOME/git/ignore when unset), then of
        root's repository's .git/info/exclude, lowest precedence first; a missing file
        contributes nothing. Outside a repository, only core.excludesFile."""

        def excludes():
            found = pygit2.discover_repository(str(root))
            if found is None:
                return _global_excludes()
            repo = pygit2.Repository(found)
            configured = None
            if "core.excludesFile" in repo.config:
                configured = pathlib.Path(
                    repo.config["core.excludesFile"]
                ).expanduser()
            info = _read_if_file(pathlib.Path(repo.path) / "info" / "exclude")
            return _excludes_file(configured) + (
                [] if info is None else [info]
            )

        return await _run(excludes)

    @staticmethod
    async def global_excludes() -> list[bytes]:
        """The contents of core.excludesFile from the global config ($XDG_CONFIG_HOME/git/ignore
        when unset)."""
        return await _run(_global_excludes)

    @staticmethod
    async def ignore_files_above(
        root: pathlib.Path, commit: GitObjectId | None
    ) -> tuple[str, list[tuple[str, bytes]]]:
        """root's path from its repository's top, "/"-terminated or "" at the top, and the
        ignore files of each directory above root, by directory from the top: on disk, or at
        commit. Outside a repository, "" and none."""

        def above():
            if pygit2.discover_repository(str(root)) is None:
                return "", []
            repo = _Repo(root)
            return repo.prefix, repo.ignore_files_above(commit)

        return await _run(above)

    @staticmethod
    def diff_contents(
        pairs: list[tuple[str, bytes | None, bytes | None]],
        context: int,
        inter_hunk_context: int,
    ) -> Diff:
        """The diff of each (path, old, new), None for an absent side, with context lines
        around each change, and changes at most inter_hunk_context lines apart in one hunk."""
        files = []
        for path, old, new in pairs:
            patch = pygit2.Patch.create_from(
                old,
                new,
                old_as_path=path,
                new_as_path=path,
                flag=_DIFF_FLAGS,
                context_lines=context,
                interhunk_lines=inter_hunk_context,
            )
            if (changed := _changed_file(patch, path)) is not None:
                files.append(changed)
        return Diff(files)

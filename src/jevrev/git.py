"""Every git command jevrev runs. Each returns a model built from git's stdout bytes, which
decides how they are decoded."""

import os
import pathlib
import typing

import pydantic
import unidiff

import jevrev.process

type GitSha = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"^[0-9a-f]{40}$")
]


class GitError(Exception):
    pass


def _is_utf8(text):
    """Whether text, decoded with surrogateescape, was valid UTF-8."""
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


class Commits(pydantic.RootModel[list[GitSha]]):
    """The commits rev-parse or merge-base prints, one per line."""

    @classmethod
    def from_output(cls, stdout: bytes):
        # A sha is ASCII; anything else fails validation, not decoding.
        return cls.model_validate(stdout.decode(errors="replace").split())


class Paths(pydantic.RootModel[list[str]]):
    """The paths ls-files -z prints, leaving out those that are not UTF-8: a review stores every
    path as text."""

    @classmethod
    def from_output(cls, stdout: bytes):
        return cls.model_validate(
            [
                path
                for path in (os.fsdecode(raw) for raw in stdout.split(b"\0"))
                if path and _is_utf8(path)
            ]
        )


class Line(pydantic.BaseModel):
    """An added or context line of a hunk."""

    added: bool
    # The line's number in the file after the change.
    number: int
    text: str


class Hunk(pydantic.BaseModel):
    source_start: int
    source_length: int
    target_start: int
    target_length: int
    # The function or section git names after the hunk's `@@` line; empty when none.
    section: str
    # Removed lines are left out: they are the old content, which nothing reviews.
    lines: list[Line]


class ChangedFile(pydantic.BaseModel):
    """A file the diff adds or modifies, by its path."""

    path: str
    hunks: list[Hunk]


class Diff(pydantic.RootModel[list[ChangedFile]]):
    """The text files a diff adds or modifies. Removed and binary files are left out, and so is a
    file whose path or added and context lines are not UTF-8: it is not text to review."""

    @classmethod
    def from_output(cls, stdout: bytes):
        files = []
        for patched in unidiff.PatchSet.from_string(
            stdout, encoding="utf-8", errors="surrogateescape"
        ):
            if patched.is_removed_file or patched.is_binary_file:
                continue
            changed = ChangedFile(
                path=patched.path,
                hunks=[
                    Hunk(
                        source_start=hunk.source_start,
                        source_length=hunk.source_length,
                        target_start=hunk.target_start,
                        target_length=hunk.target_length,
                        section=hunk.section_header,
                        lines=[
                            Line(
                                added=line.is_added,
                                number=line.target_line_no,
                                text=line.value.removesuffix("\n"),
                            )
                            for line in hunk
                            if line.is_added or line.is_context
                        ],
                    )
                    for hunk in patched
                ],
            )
            if _is_utf8(changed.path) and all(
                _is_utf8(hunk.section) and _is_utf8(line.text)
                for hunk in changed.hunks
                for line in hunk.lines
            ):
                files.append(changed)
        return cls(files)


class git:
    @staticmethod
    async def _run(root: pathlib.Path, argv: list[str]) -> bytes:
        """The stdout of git, in root, with argv."""
        try:
            return await jevrev.process.run(
                ["git", "--no-pager", "-C", str(root), *argv]
            )
        except jevrev.process.ProcessError as e:
            raise GitError(str(e)) from e

    @staticmethod
    async def top_level(root: pathlib.Path) -> pathlib.Path:
        """The top directory of the work tree root is in."""
        stdout = await git._run(root, ["rev-parse", "--show-toplevel"])
        return pathlib.Path(os.fsdecode(stdout.removesuffix(b"\n")))

    @staticmethod
    async def in_work_tree(root: pathlib.Path) -> bool:
        try:
            stdout = await git._run(
                root, ["rev-parse", "--is-inside-work-tree"]
            )
        except GitError:
            return False
        return stdout.strip() == b"true"

    @staticmethod
    async def fetch(root: pathlib.Path, refspec: str):
        """Fetch refspec from origin."""
        await git._run(root, ["fetch", "--quiet", "origin", refspec])

    @staticmethod
    async def rev_parse(
        root: pathlib.Path, revisions: list[str]
    ) -> list[GitSha]:
        """The commit each of revisions names, in order."""
        return Commits.from_output(
            await git._run(root, ["rev-parse", *revisions])
        ).root

    @staticmethod
    async def merge_base(
        root: pathlib.Path, first: str, second: str
    ) -> GitSha:
        (commit,) = Commits.from_output(
            await git._run(root, ["merge-base", first, second])
        ).root
        return commit

    @staticmethod
    async def checkout_detached(root: pathlib.Path, revision: str):
        await git._run(root, ["checkout", "--quiet", "--detach", revision])

    @staticmethod
    async def diff_work_tree(
        root: pathlib.Path, revision: str, context: int, pathspecs: list[str]
    ) -> Diff:
        """The diff from revision to the work tree of the files matching pathspecs, with context
        lines around each change and paths relative to root."""
        return Diff.from_output(
            await git._run(
                root,
                [
                    "diff",
                    f"-U{context}",
                    "--no-color",
                    "--no-ext-diff",
                    "--relative",
                    revision,
                    "--",
                    *pathspecs,
                ],
            )
        )

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
        return Diff.from_output(
            await git._run(
                root,
                [
                    "diff",
                    "--no-color",
                    "--no-ext-diff",
                    f"-U{context}",
                    f"--inter-hunk-context={inter_hunk_context}",
                    base,
                    head,
                ],
            )
        )

    @staticmethod
    async def files(root: pathlib.Path, pathspecs: list[str]) -> list[str]:
        """The tracked and untracked files matching pathspecs, leaving out those git ignores."""
        return Paths.from_output(
            await git._run(
                root,
                [
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *pathspecs,
                ],
            )
        ).root

    @staticmethod
    async def untracked_files(
        root: pathlib.Path, pathspecs: list[str]
    ) -> list[str]:
        """The untracked files matching pathspecs, leaving out those git ignores."""
        return Paths.from_output(
            await git._run(
                root,
                [
                    "ls-files",
                    "-z",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *pathspecs,
                ],
            )
        ).root

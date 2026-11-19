"""A review's file: JSON holding the review's source, the version of it reviewed, its metadata
and its findings."""

import contextlib
import enum
import fcntl
import os
import pathlib
import typing

import pydantic

import jevrev.git
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.jev.scan
import jevrev.state


class InvalidReview(Exception):
    """The review file is not a valid review, or another command holds its lock."""


class Verdict(enum.StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


# A finding's position in the scan's findings, from 1.
type FindingIndex = typing.Annotated[int, pydantic.Field(ge=1)]

type VerdictReason = str


class Finding(jevrev.jev.scan.Finding):
    """A jevrev.jev.scan finding, its file relative to the review's source, with its index and
    its verdict once it has one."""

    index: FindingIndex
    verdict: Verdict | None = None
    reason: VerdictReason | None = None

    @pydantic.model_validator(mode="after")
    def _verdict_has_reason(self):
        if (self.verdict is None) != (self.reason is None):
            raise ValueError(
                f"finding {self.index}: verdict and reason are both null "
                "or both given"
            )
        return self


type MetadataKey = typing.Annotated[
    str, pydantic.StringConstraints(min_length=1)
]

# A --metadata value given to scan; may be empty.
type MetadataValue = str

# An --include or --exclude pattern given to scan, in gitignore syntax.
type Pattern = str


class LocalDir(pydantic.BaseModel):
    """A directory, or one file, reviewed as its files on disk."""

    model_config = pydantic.ConfigDict(extra="forbid")

    path: pathlib.Path


class LocalGitDir(pydantic.BaseModel):
    """A git directory reviewed at git."""

    model_config = pydantic.ConfigDict(extra="forbid")

    path: pathlib.Path
    git: jevrev.git.GitRev | jevrev.git.GitDiff | jevrev.git.GitDiffToDisk


class GithubRepo(jevrev.github.repo.GithubRepo):
    """A GitHub repo, on the host scan-github resolved, reviewed at git."""

    git: (
        jevrev.git.GitRev
        | jevrev.git.GitDiff
        | jevrev.github.pull_request.GithubPullRequest
    )


class Review(pydantic.BaseModel):
    """One scan and the verdicts on its findings; findings' files are relative to the
    source."""

    model_config = pydantic.ConfigDict(extra="forbid")

    source: LocalDir | LocalGitDir | GithubRepo
    metadata: dict[MetadataKey, MetadataValue] = {}
    # The --include and --exclude patterns, in the order given.
    include: list[Pattern] = []
    exclude: list[Pattern] = []
    # jevrev.jev.scan's findings, in its order.
    findings: list[Finding]


class Store:
    """A review's file under its lock."""

    def __init__(self, file: typing.BinaryIO):
        self.file = file

    def load(self) -> Review:
        """Raises InvalidReview when the file is not a valid review."""
        self.file.seek(0)
        try:
            return Review.model_validate_json(self.file.read())
        except pydantic.ValidationError as e:
            raise InvalidReview(f"review file: {e}") from e

    def save(self, review: Review):
        """Replace the file's contents with review."""
        self.file.seek(0)
        self.file.truncate()
        self.file.write(review.model_dump_json().encode())
        self.file.flush()
        os.fsync(self.file.fileno())


@contextlib.contextmanager
def open_review(path: pathlib.Path):
    """The review file at path, created empty when missing, under an exclusive lock until
    exit; fails at once with "the review file is locked" when another command holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    with os.fdopen(fd, "r+b") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InvalidReview("the review file is locked") from None
        yield Store(file)

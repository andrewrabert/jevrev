"""A review's SQLite db: the review's source, the version of it reviewed, its metadata and its
findings, each in a table of its own."""

import contextlib
import enum
import pathlib
import sqlite3
import typing

import pydantic

import jevrev.git
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.jev.scan
import jevrev.state

# Raised when the db can't be opened or read, or another command holds its lock.
OperationalError = sqlite3.OperationalError


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


type MetadataKey = typing.Annotated[
    str, pydantic.StringConstraints(min_length=1)
]

# A --metadata value given to scan; may be empty.
type MetadataValue = str

# An --include or --exclude pattern given to scan, in gitignore syntax.
type Pattern = str


class PatternKind(enum.StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


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


def _one_of(column: str, values: type[enum.StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


# One single-row table for each kind of source and each kind of selection: a review's db holds
# one source row and, unless the source is a LocalDir, one selection row.
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY CHECK (id = 0)
) STRICT;

CREATE TABLE IF NOT EXISTS local_dir (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    path TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS local_git_dir (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    path TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS github_repo (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    host TEXT NOT NULL,
    org TEXT NOT NULL,
    name TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS git_rev (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    "commit" TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS git_diff (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    base TEXT NOT NULL,
    head TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS git_diff_to_disk (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    base TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS github_pull_request (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES git_diff (id),
    number INTEGER NOT NULL CHECK (number > 0)
) STRICT;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS pattern (
    position INTEGER PRIMARY KEY CHECK (position >= 1),
    kind TEXT NOT NULL CHECK ({_one_of("kind", PatternKind)}),
    pattern TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS finding (
    "index" INTEGER PRIMARY KEY CHECK ("index" >= 1),
    file TEXT NOT NULL,
    start INTEGER NOT NULL,
    "end" INTEGER NOT NULL,
    item TEXT NOT NULL,
    rule TEXT NOT NULL,
    probability REAL NOT NULL,
    flags TEXT NOT NULL,
    violation TEXT NOT NULL,
    accepted TEXT NOT NULL,
    evaluated TEXT CHECK ({_one_of("evaluated", jevrev.state.Evaluation)}),
    verdict TEXT CHECK ({_one_of("verdict", Verdict)}),
    reason TEXT,
    CHECK ((verdict IS NULL) = (reason IS NULL))
) STRICT;
"""


class _Row(pydantic.BaseModel):
    """A row of table, its fields its columns."""

    model_config = pydantic.ConfigDict(extra="forbid")

    table: typing.ClassVar[str]


class _SingleRow(_Row):
    """A row of a table that holds at most one."""

    id: typing.Literal[0] = 0


class _ReviewRow(_SingleRow):
    table = "review"


class _LocalDirRow(_SingleRow):
    table = "local_dir"

    path: pathlib.Path


class _LocalGitDirRow(_SingleRow):
    table = "local_git_dir"

    path: pathlib.Path


class _GithubRepoRow(_SingleRow, jevrev.github.repo.GithubRepo):
    table = "github_repo"


class _GitRevRow(_SingleRow, jevrev.git.GitRev):
    table = "git_rev"


class _GitDiffRow(_SingleRow, jevrev.git.GitDiff):
    table = "git_diff"


class _GitDiffToDiskRow(_SingleRow, jevrev.git.GitDiffToDisk):
    table = "git_diff_to_disk"


class _GithubPullRequestRow(_SingleRow):
    table = "github_pull_request"

    number: jevrev.github.pull_request.GithubPRNumber


class _MetadataRow(_Row):
    table = "metadata"

    key: MetadataKey
    value: MetadataValue


class _PatternRow(_Row):
    table = "pattern"

    # The pattern's position among the review's patterns, from 1.
    position: typing.Annotated[int, pydantic.Field(ge=1)]
    kind: PatternKind
    pattern: Pattern


class _FindingRow(_Row, Finding):
    table = "finding"


# Child tables first, so a delete never leaves a row its parent's delete would orphan.
_ROWS: tuple[type[_Row], ...] = (
    _FindingRow,
    _PatternRow,
    _MetadataRow,
    _GithubPullRequestRow,
    _GitDiffToDiskRow,
    _GitDiffRow,
    _GitRevRow,
    _GithubRepoRow,
    _LocalGitDirRow,
    _LocalDirRow,
    _ReviewRow,
)


def _quoted(columns: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in columns)


class Store:
    """A review in its db."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def _rows[R: _Row](
        self, model: type[R], order: str | None = None
    ) -> list[R]:
        """Every row of model's table, each validated as model; order names the column they are
        sorted by."""
        columns = list(model.model_fields)
        query = f"SELECT {_quoted(columns)} FROM {model.table}"
        if order is not None:
            query += f" ORDER BY {_quoted([order])}"
        try:
            return [
                model.model_validate(dict(row))
                for row in self.db.execute(query)
            ]
        except pydantic.ValidationError as e:
            raise OperationalError(f"{model.table}: {e}") from e

    def _single[R: _SingleRow](self, model: type[R]) -> R | None:
        rows = self._rows(model)
        return rows[0] if rows else None

    def _insert(self, row: _Row):
        values = row.model_dump(mode="json")
        self.db.execute(
            f"INSERT INTO {row.table} ({_quoted(list(values))}) "
            f"VALUES ({', '.join(':' + column for column in values)})",
            values,
        )

    def _selection(
        self,
    ) -> (
        jevrev.git.GitRev
        | jevrev.git.GitDiff
        | jevrev.git.GitDiffToDisk
        | jevrev.github.pull_request.GithubPullRequest
        | None
    ):
        found = []
        if rev := self._single(_GitRevRow):
            found.append(jevrev.git.GitRev(commit=rev.commit))
        if diff := self._single(_GitDiffRow):
            if pr := self._single(_GithubPullRequestRow):
                found.append(
                    jevrev.github.pull_request.GithubPullRequest(
                        base=diff.base, head=diff.head, number=pr.number
                    )
                )
            else:
                found.append(
                    jevrev.git.GitDiff(base=diff.base, head=diff.head)
                )
        if to_disk := self._single(_GitDiffToDiskRow):
            found.append(jevrev.git.GitDiffToDisk(base=to_disk.base))
        if len(found) > 1:
            raise OperationalError("the db holds more than one selection")
        return found[0] if found else None

    def _source(self) -> LocalDir | LocalGitDir | GithubRepo:
        selection = self._selection()
        found = []
        if local := self._single(_LocalDirRow):
            if selection is not None:
                raise OperationalError(
                    "the db holds a selection of a local dir"
                )
            found.append(LocalDir(path=local.path))
        if local_git := self._single(_LocalGitDirRow):
            found.append(LocalGitDir(path=local_git.path, git=selection))
        if github := self._single(_GithubRepoRow):
            found.append(
                GithubRepo(
                    host=github.host,
                    org=github.org,
                    name=github.name,
                    git=selection,
                )
            )
        if len(found) != 1:
            raise OperationalError("the db does not hold exactly one source")
        return found[0]

    def load(self) -> Review | None:
        if self._single(_ReviewRow) is None:
            return None
        try:
            source = self._source()
        except pydantic.ValidationError as e:
            raise OperationalError(
                f"the db holds an invalid source: {e}"
            ) from e
        patterns = self._rows(_PatternRow, order="position")
        return Review(
            source=source,
            metadata={
                row.key: row.value
                for row in self._rows(_MetadataRow, order="key")
            },
            include=[
                row.pattern
                for row in patterns
                if row.kind == PatternKind.INCLUDE
            ],
            exclude=[
                row.pattern
                for row in patterns
                if row.kind == PatternKind.EXCLUDE
            ],
            findings=[
                Finding.model_validate(row.model_dump())
                for row in self._rows(_FindingRow, order="index")
            ],
        )

    def _save_selection(
        self,
        selection: jevrev.git.GitRev
        | jevrev.git.GitDiff
        | jevrev.git.GitDiffToDisk,
    ):
        match selection:
            case jevrev.git.GitRev(commit=commit):
                self._insert(_GitRevRow(commit=commit))
            case jevrev.git.GitDiff(base=base, head=head):
                self._insert(_GitDiffRow(base=base, head=head))
                if isinstance(
                    selection, jevrev.github.pull_request.GithubPullRequest
                ):
                    self._insert(
                        _GithubPullRequestRow(number=selection.number)
                    )
            case jevrev.git.GitDiffToDisk(base=base):
                self._insert(_GitDiffToDiskRow(base=base))

    def save(self, review: Review):
        """Replace whatever review the db holds with review."""
        for model in _ROWS:
            self.db.execute(f"DELETE FROM {model.table}")
        self._insert(_ReviewRow())
        match review.source:
            case LocalDir(path=path):
                self._insert(_LocalDirRow(path=path))
            case LocalGitDir(path=path, git=selection):
                self._insert(_LocalGitDirRow(path=path))
                self._save_selection(selection)
            case GithubRepo(host=host, org=org, name=name, git=selection):
                self._insert(_GithubRepoRow(host=host, org=org, name=name))
                self._save_selection(selection)
        for key, value in review.metadata.items():
            self._insert(_MetadataRow(key=key, value=value))
        patterns = [(PatternKind.INCLUDE, p) for p in review.include] + [
            (PatternKind.EXCLUDE, p) for p in review.exclude
        ]
        for position, (kind, pattern) in enumerate(patterns, 1):
            self._insert(
                _PatternRow(position=position, kind=kind, pattern=pattern)
            )
        for finding in review.findings:
            self._insert(_FindingRow.model_validate(finding.model_dump()))


@contextlib.contextmanager
def open_db(path: pathlib.Path):
    """The db at path, holding its write lock until exit. Fails at once if
    another command holds the lock."""
    with contextlib.closing(
        sqlite3.connect(path, timeout=0, autocommit=True)
    ) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield Store(db)
        finally:
            db.execute("COMMIT")

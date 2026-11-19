"""A review's SQLite db: the review, the pull request it is of, and its
findings, each in a table of its own."""

import contextlib
import enum
import pathlib
import sqlite3

import pydantic

import jevrev.git
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.jev.scan
import jevrev.state

# Raised when the db can't be opened, read, or locked in time.
OperationalError = sqlite3.OperationalError


class Verdict(enum.StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Finding(jevrev.jev.scan.Finding):
    """A jevrev.jev.scan finding, its file relative to the review's dir, with its
    index in the scan's findings, from 1, and its verdict once it has one."""

    index: int
    verdict: Verdict | None = None
    reason: str | None = None


class PullRequestSource(pydantic.BaseModel):
    """The pull request a review's dir is a clone of, at its head."""

    repo: jevrev.github.repo.GithubRepo
    pr: jevrev.github.pull_request.GithubPRNumber
    head_sha: jevrev.git.GitSha
    target: str
    target_sha: jevrev.git.GitSha
    merge_base: jevrev.git.GitSha


class Review(pydantic.BaseModel):
    """One scan of a directory and the verdicts on its findings."""

    model_config = pydantic.ConfigDict(extra="forbid")

    dir: pathlib.Path
    # The revision the change is from; None when full.
    base: str | None = None
    # Whether jev scanned every file of the dir, not the change from base.
    full: bool = False
    # Set when the dir is a clone of a pull request.
    pull_request: PullRequestSource | None = None
    # The --metadata KEY=VALUE pairs given to scan.
    metadata: dict[str, str] = {}
    # jevrev.jev.scan's findings, in its order.
    findings: list[Finding]


def _one_of(column: str, values: type[enum.StrEnum]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    dir TEXT NOT NULL,
    base TEXT,
    full INTEGER NOT NULL CHECK (full IN (0, 1))
) STRICT;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS pull_request (
    id INTEGER PRIMARY KEY CHECK (id = 0) REFERENCES review (id),
    host TEXT NOT NULL,
    org TEXT NOT NULL,
    name TEXT NOT NULL,
    pr INTEGER NOT NULL CHECK (pr > 0),
    head_sha TEXT NOT NULL,
    target TEXT NOT NULL,
    target_sha TEXT NOT NULL,
    merge_base TEXT NOT NULL
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

_FINDING_COLUMNS = list(Finding.model_fields)
_PULL_REQUEST_COLUMNS = [
    "host",
    "org",
    "name",
    *(f for f in PullRequestSource.model_fields if f != "repo"),
]


def _quoted(columns: list[str]) -> str:
    return ", ".join(f'"{c}"' for c in columns)


class Store:
    """A review in its db."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def load(self) -> Review | None:
        """The review; None when the db holds no review."""
        row = self.db.execute("SELECT dir, base, full FROM review").fetchone()
        if row is None:
            return None
        pull_request = None
        if pr := self.db.execute(
            f"SELECT {_quoted(_PULL_REQUEST_COLUMNS)} FROM pull_request"
        ).fetchone():
            pull_request = PullRequestSource(
                repo=jevrev.github.repo.GithubRepo(
                    host=pr["host"], org=pr["org"], name=pr["name"]
                ),
                pr=pr["pr"],
                head_sha=pr["head_sha"],
                target=pr["target"],
                target_sha=pr["target_sha"],
                merge_base=pr["merge_base"],
            )
        return Review(
            dir=row["dir"],
            base=row["base"],
            full=row["full"],
            pull_request=pull_request,
            metadata=dict(
                self.db.execute("SELECT key, value FROM metadata ORDER BY key")
            ),
            findings=[
                Finding.model_validate(dict(finding))
                for finding in self.db.execute(
                    f'SELECT {_quoted(_FINDING_COLUMNS)} FROM finding ORDER BY "index"'
                )
            ],
        )

    def save(self, review: Review):
        """Replace whatever review the db holds with review."""
        self.db.execute("DELETE FROM finding")
        self.db.execute("DELETE FROM pull_request")
        self.db.execute("DELETE FROM metadata")
        self.db.execute("DELETE FROM review")
        self.db.execute(
            "INSERT INTO review (id, dir, base, full) VALUES (0, ?, ?, ?)",
            (str(review.dir), review.base, review.full),
        )
        self.db.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            review.metadata.items(),
        )
        if (source := review.pull_request) is not None:
            row = {
                **source.repo.model_dump(mode="json"),
                **source.model_dump(mode="json", exclude={"repo"}),
            }
            self.db.execute(
                f"INSERT INTO pull_request (id, {_quoted(_PULL_REQUEST_COLUMNS)}) "
                f"VALUES (0, {', '.join(':' + c for c in _PULL_REQUEST_COLUMNS)})",
                row,
            )
        self.db.executemany(
            f"INSERT INTO finding ({_quoted(_FINDING_COLUMNS)}) "
            f"VALUES ({', '.join(':' + c for c in _FINDING_COLUMNS)})",
            [finding.model_dump(mode="json") for finding in review.findings],
        )


@contextlib.contextmanager
def open_db(path: pathlib.Path, lock_timeout: float):
    """The db at path, holding its write lock until exit."""
    with contextlib.closing(
        sqlite3.connect(path, timeout=lock_timeout, autocommit=True)
    ) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield Store(db)
        finally:
            db.execute("COMMIT")

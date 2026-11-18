import argparse
import asyncio
import contextlib
import datetime
import enum
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import typing

import minijinja
import pydantic
import unidiff
import yaml

import github.gh
import github.pull_request
import github.repo
import jev_pr_review.jev_ask
import jev_pr_review.jev_scan
import jev_pr_review.process
import jev_pr_review.sandbox
import jev_pr_review.templates

# Seconds a jev scan may run.
JEV_SCAN_TIMEOUT = 600
# Requests to jev a scan has open at once.
JEV_SCAN_CONCURRENCY = 8
PREPARED = "prepared"
HEAD = "head"
USERNAME = "agent"
HOSTNAME = "sandbox"
HOME_DIR = pathlib.PurePosixPath("/sandbox")
REPOS = "repos"
REPOS_DIR = HOME_DIR / REPOS
LOCAL_REPOS_DIR = REPOS_DIR / "local"
JEV_FINDINGS = "jev-findings.jsonl"
JEV_FINDINGS_PATH = HOME_DIR / JEV_FINDINGS

BUILD_ONLY_ENV = (
    "BASHKIT_ALLOW_INPROCESS_PYTHON",
    "BASHKIT_ALLOW_INPROCESS_SQLITE",
)

# The review's tables in its state file, beside the sandbox's snapshot.
SCHEMA = """
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file (
    name TEXT PRIMARY KEY,
    data BLOB NOT NULL
);
"""


async def run(*args):
    args = tuple(map(str, args))
    proc = await jev_pr_review.process.start(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await jev_pr_review.process.communicate(proc)
    if proc.returncode != 0:
        raise ProcessError(
            proc,
            f"{' '.join(args[:3])}\n{stderr.decode().strip()}",
        )
    return stdout.decode()


async def git(git_dir, *argv):
    return await run("git", "--no-pager", f"--git-dir={git_dir}", *argv)


def local_repo_path(git_dir: pathlib.Path) -> pathlib.PurePosixPath:
    # The worktree of a .git dir, or the bare repo, mirrored by its host path.
    path = git_dir.parent if git_dir.name == ".git" else git_dir
    return LOCAL_REPOS_DIR / path.relative_to(path.anchor)


class SourceKind(enum.StrEnum):
    GITHUB = "github"
    LOCAL = "local"


class GithubSource(pydantic.BaseModel):
    kind: typing.Literal[SourceKind.GITHUB] = SourceKind.GITHUB
    repo: github.repo.GithubRepo
    pr: github.pull_request.GithubPRNumber
    head_sha: github.pull_request.GitSha
    target: str
    target_sha: github.pull_request.GitSha
    merge_base: github.pull_request.GitSha
    clone: pathlib.Path | None = pydantic.Field(default=None, exclude=True)

    def base_label(self) -> str:
        return f"merge base with {self.target}"

    def repos(self) -> dict[pathlib.PurePosixPath, pathlib.Path]:
        return {
            REPOS_DIR / self.repo.host / self.repo.org / self.repo.name: (
                self.clone
            )
        }


class LocalSource(pydantic.BaseModel):
    kind: typing.Literal[SourceKind.LOCAL] = SourceKind.LOCAL
    git_dir: pathlib.Path
    # None once loaded from a stored result.
    clone: pathlib.Path | None = pydantic.Field(default=None, exclude=True)
    base_rev: str
    head_rev: str
    base_commit: str
    head_commit: str

    def base_label(self) -> str:
        return self.base_rev

    def repos(self) -> dict[pathlib.PurePosixPath, pathlib.Path]:
        return {local_repo_path(self.git_dir): self.clone}


class JevVerdict(enum.StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class JevInlineFinding(pydantic.BaseModel):
    """One jev_scan finding of a jev inline review, and its verdict."""

    # Relative to the head.
    path: str
    start: int
    end: int
    evaluated: jev_pr_review.jev_scan.Evaluation | None = None
    rule: str
    flags: str
    verdict: JevVerdict | None = None
    reason: str | None = None


class JevInlineReviewResult(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    source: GithubSource | LocalSource = pydantic.Field(discriminator="kind")
    # Whether jev scanned every file of the head, not the change from the
    # base.
    full: bool = False
    # The state file holding the review.
    state: pathlib.Path
    started: datetime.datetime
    finished: datetime.datetime
    comment_url: str | None = None
    findings: list[JevInlineFinding]


def now():
    return datetime.datetime.now(datetime.UTC)


class UserError(Exception):
    pass


class ProcessError(Exception):
    def __init__(self, process, message=None):
        self.process = process
        self.message = message

    def __str__(self):
        text = f"exit {self.process.returncode}"
        if self.message:
            text = f"{text} - {self.message}"
        return text


class InputKind(enum.StrEnum):
    GITHUB = "github"
    GITHUB_FULL = "github-full"
    LOCAL = "local"
    LOCAL_FULL = "local-full"


class AgentInput(pydantic.BaseModel):
    """The input an agent is spawned with: its prompt, as JSON."""

    model_config = pydantic.ConfigDict(extra="forbid")


class GithubPullRequestInput(AgentInput):
    repo: github.repo.GithubRepo = pydantic.Field(description="The repo")
    pr: github.pull_request.GithubPRNumber = pydantic.Field(
        description="Pull request number"
    )


class GithubJevInlineReviewInput(GithubPullRequestInput):
    kind: typing.Literal[InputKind.GITHUB] = pydantic.Field(
        description="A pull request"
    )


class LocalJevInlineReviewInput(AgentInput):
    kind: typing.Literal[InputKind.LOCAL] = pydantic.Field(
        description="Two revisions of a git dir on the host"
    )
    git_dir: str = pydantic.Field(
        description="Path of the git dir on the host"
    )
    base: str = pydantic.Field(description="Revision to compare from")
    head: str = pydantic.Field(description="Revision to audit")


class GithubFullJevInlineReviewInput(GithubPullRequestInput):
    kind: typing.Literal[InputKind.GITHUB_FULL] = pydantic.Field(
        description="Every file of a pull request's repo at its head, "
        "reviewed on the pull request"
    )


class LocalFullJevInlineReviewInput(AgentInput):
    kind: typing.Literal[InputKind.LOCAL_FULL] = pydantic.Field(
        description="Every file of a git dir on the host at a revision"
    )
    git_dir: str = pydantic.Field(
        description="Path of the git dir on the host"
    )
    head: str = pydantic.Field(description="Revision to audit")


JevInlineReviewInput = typing.Annotated[
    GithubJevInlineReviewInput
    | GithubFullJevInlineReviewInput
    | LocalJevInlineReviewInput
    | LocalFullJevInlineReviewInput,
    pydantic.Field(discriminator="kind"),
]


REVIEW_INPUT = pydantic.TypeAdapter(JevInlineReviewInput)


def validation_error(error: pydantic.ValidationError):
    lines = (
        f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}"
        for e in error.errors(include_url=False)
    )
    return UserError("Invalid review input:\n" + "\n".join(lines))


def text_lines(text):
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def source_commits(source: GithubSource | LocalSource) -> tuple[str, str]:
    """The commits a review of source compares: its base and its head."""
    match source:
        case GithubSource():
            return source.merge_base, source.head_sha
        case LocalSource():
            return source.base_commit, source.head_commit


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    FINISHED = "finished"
    CANCELLED = "cancelled"


class LineRange(pydantic.BaseModel):
    """Lines start to stop - 1 of a file."""

    start: int
    stop: int


class JevInlineReview(pydantic.BaseModel):
    """A jev inline review, as its state file holds it."""

    input: JevInlineReviewInput
    status: ReviewStatus = ReviewStatus.OPEN
    started: datetime.datetime = pydantic.Field(default_factory=now)
    finished: datetime.datetime | None = None
    source: GithubSource | LocalSource = pydantic.Field(discriminator="kind")
    # Where the sandbox mounts the working tree of the head.
    mount: pathlib.PurePosixPath
    # The directory holding the review's clone and head checkout, from
    # start-review until it finishes or is cancelled.
    clone_dir: pathlib.Path
    # A pull request review's github_hunks, captured at start-review.
    github_hunks: dict[str, list[LineRange]] | None = None
    # Whether jev scans every file of the head, not the change from the base.
    full: bool = False
    comment_url: str | None = None
    # jev_scan's findings, in the order of its file.
    findings: list[JevInlineFinding]

    def require_status(self, status: ReviewStatus):
        if self.status != status:
            raise UserError(f"The review is {self.status}, not {status}.")

    def base_commit(self) -> str:
        return source_commits(self.source)[0]

    def head_commit(self) -> str:
        return source_commits(self.source)[1]

    def result(self, state: pathlib.Path) -> JevInlineReviewResult:
        return JevInlineReviewResult(
            source=self.source,
            state=state.resolve(),
            full=self.full,
            started=self.started,
            finished=self.finished or now(),
            comment_url=self.comment_url,
            findings=self.findings,
        )


class ReviewState:
    """A review's tables in its state file, beside its sandbox's snapshot."""

    def __init__(self, state: jev_pr_review.sandbox.State, path: pathlib.Path):
        self.state = state
        self.path = path
        self.db: sqlite3.Connection = state.db
        self.db.executescript(SCHEMA)

    def load(self) -> JevInlineReview:
        row = self.db.execute("SELECT data FROM review").fetchone()
        if row is None:
            raise UserError("The state file holds no review.")
        return JevInlineReview.model_validate_json(row[0])

    def save(self, review: JevInlineReview):
        self.db.execute(
            "INSERT OR REPLACE INTO review (id, data) VALUES (0, ?)",
            (review.model_dump_json(),),
        )

    def read_file(self, name) -> bytes | None:
        row = self.db.execute(
            "SELECT data FROM file WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else row[0]

    def write_file(self, name, data: bytes):
        self.db.execute(
            "INSERT OR REPLACE INTO file (name, data) VALUES (?, ?)",
            (name, data),
        )


@contextlib.contextmanager
def open_review(path: pathlib.Path, lock_timeout: float):
    """The review in the state file at path, holding its write lock until
    exit."""
    if not path.is_file():
        raise UserError(f"{path}: no such state file")
    with jev_pr_review.sandbox.State.open(path, lock_timeout) as state:
        yield ReviewState(state, path)


def prepare_home(root: pathlib.Path, mount: pathlib.PurePosixPath):
    """The host directory mounted as the sandbox's home, holding the mount
    point of the head."""
    prepared = root / PREPARED
    (prepared / mount.relative_to(HOME_DIR)).mkdir(parents=True)
    return prepared


def review_checkout(review: JevInlineReview) -> pathlib.Path:
    """The head checkout of the review's clone."""
    root = review.clone_dir
    if not (root / HEAD).is_dir():
        raise UserError(
            f"The review's clone at {root} is gone; cancel this review and "
            "start a new one."
        )
    review.source.clone = root / HEAD / ".git"
    return root / HEAD


def remove_clone(review: JevInlineReview):
    shutil.rmtree(review.clone_dir, ignore_errors=True)


def opened_sandbox(store: "ReviewState", prepared: pathlib.Path, repos):
    """The review's bashkit sandbox, with its home and each of repos mounted
    read-only, saved to the state file on the way out."""
    mounts = [
        jev_pr_review.sandbox.Mount(host_path=host, bashkit_path=path)
        for path, host in ((HOME_DIR, prepared), *repos.items())
    ]
    return jev_pr_review.sandbox.Sandbox.opened(
        store.state, mounts, USERNAME, HOSTNAME, str(HOME_DIR)
    )


@contextlib.contextmanager
def sandbox_home(review: JevInlineReview, findings: bytes | None):
    """The sandbox's home on the host, holding jev_scan's findings, in a
    temporary directory deleted on exit."""
    with tempfile.TemporaryDirectory(prefix="jev-code-review-home-") as tmp:
        prepared = prepare_home(pathlib.Path(tmp).resolve(), review.mount)
        if findings is not None:
            (prepared / JEV_FINDINGS).write_bytes(findings)
        yield prepared


def review_input(args) -> JevInlineReviewInput:
    """The review start-review's flags describe."""
    given = {
        name: value
        for name in ("pr", "git_dir", "base", "head")
        if (value := getattr(args, name)) is not None
    }
    if args.repo is not None:
        host, *rest = args.repo.split("/")
        if len(rest) != 2:
            raise UserError("--repo: expected HOST/ORG/NAME")
        given["repo"] = {"host": host, "org": rest[0], "name": rest[1]}
    if "repo" in given:
        kind = InputKind.GITHUB_FULL if args.full else InputKind.GITHUB
    elif "git_dir" in given:
        kind = InputKind.LOCAL_FULL if args.full else InputKind.LOCAL
    else:
        raise UserError("Give --repo and --pr, or --git-dir.")
    try:
        return REVIEW_INPUT.validate_python({"kind": kind, **given})
    except pydantic.ValidationError as e:
        raise validation_error(e)


async def prepare_review(root, value: JevInlineReviewInput):
    """value's source and head checkout under root, and whether it is full."""
    match value:
        case GithubJevInlineReviewInput():
            full = False
        case GithubFullJevInlineReviewInput():
            full = True
        case LocalJevInlineReviewInput():
            source, checkout = await prepare_local_checkout(
                root, pathlib.Path(value.git_dir), value.base, value.head
            )
            return source, checkout, False
        case LocalFullJevInlineReviewInput():
            # A full review scans no change, so its base is its head.
            source, checkout = await prepare_local_checkout(
                root, pathlib.Path(value.git_dir), value.head, value.head
            )
            return source, checkout, True
    source, checkout = await prepare_github_checkout(
        root, value.repo, value.pr
    )
    return source, checkout, full


async def start_review(args):
    state: pathlib.Path = args.state
    if state.exists():
        raise UserError(f"{state} already exists")
    if args.rules is None:
        raise UserError("Give --rules or set JEV_RULES_DIR.")
    value = review_input(args)
    parent = (
        pathlib.Path(tempfile.gettempdir())
        if args.cache_dir is None
        else args.cache_dir
    )
    parent.mkdir(parents=True, exist_ok=True)
    # Resolved, as jev-scan prints the paths of its findings.
    root = pathlib.Path(
        tempfile.mkdtemp(prefix="jev-code-review-", dir=parent)
    ).resolve()
    try:
        source, checkout, full = await prepare_review(root, value)
        (mount,) = source.repos()
        findings, findings_file, summary = await scan(
            source, checkout, mount, full, args.rules, args.extra_rules
        )
        review = JevInlineReview(
            input=value,
            source=source,
            mount=mount,
            clone_dir=root,
            github_hunks=(
                {
                    path: [
                        LineRange(start=r.start, stop=r.stop) for r in ranges
                    ]
                    for path, ranges in (await github_hunks(source)).items()
                }
                if isinstance(source, GithubSource)
                else None
            ),
            full=full,
            findings=findings,
        )
        with (
            jev_pr_review.sandbox.State.open(
                state, args.lock_timeout
            ) as opened,
            sandbox_home(review, None) as prepared,
        ):
            store = ReviewState(opened, state)
            with opened_sandbox(store, prepared, {mount: checkout}) as sandbox:
                result = sandbox.bash.execute_sync(
                    f"export HOME={HOME_DIR}; unset {' '.join(BUILD_ONLY_ENV)}"
                )
            if result.exit_code != 0:
                raise UserError(result.stderr)
            store.save(review)
            # Written to the sandbox, not printed: a large scan's result is
            # more than a caller can take back as one command's output.
            store.write_file(JEV_FINDINGS, findings_file.encode())
    except BaseException:
        state.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)
        raise
    print(f"{task_brief(review)}\n{JEV_FINDINGS_PATH}\n{summary}")


async def scan(source, checkout, mount, full, rules, extra_rules):
    """jev_scan's findings on the head checkout: as findings, as the JSON
    lines of its file in the sandbox, and its summary line."""
    findings, summary = await run_jev_scan(
        checkout,
        rules,
        extra_rules,
        **(
            {}
            if full
            else {
                "base": source_commits(source)[0],
                "evaluate": [
                    jev_pr_review.jev_scan.Evaluation.HUNK,
                    jev_pr_review.jev_scan.Evaluation.FILE,
                ],
            }
        ),
    )
    lines = list(at_mount(findings, checkout, mount))
    found = [
        inline_finding(
            jev_pr_review.jev_scan.Finding.model_validate_json(line),
            checkout,
        )
        for line in text_lines(findings)
    ]
    lines = [
        json.dumps({**json.loads(line), "index": index}, sort_keys=True)
        for index, line in enumerate(lines, 1)
    ]
    return (
        found,
        "".join(f"{line}\n" for line in lines),
        summary.replace(str(checkout), str(mount)),
    )


async def confirm_finding(args):
    await judge_finding(args, JevVerdict.CONFIRMED)


async def reject_finding(args):
    await judge_finding(args, JevVerdict.REJECTED)


async def judge_finding(args, verdict: JevVerdict):
    index = args.index
    with open_review(args.state, args.lock_timeout) as store:
        task = store.load()
        task.require_status(ReviewStatus.OPEN)
        count = len(task.findings)
        if not 1 <= index <= count:
            raise UserError(f"index: {index} is not in 1..{count}")
        finding = task.findings[index - 1]
        finding.verdict = verdict
        finding.reason = args.reason
        store.save(task)
    print(f"Finding {index} {verdict}")


async def finish_review(args):
    with open_review(args.state, args.lock_timeout) as store:
        task = store.load()
        task.require_status(ReviewStatus.OPEN)
        if unjudged := [
            str(index)
            for index, finding in enumerate(task.findings, 1)
            if finding.verdict is None
        ]:
            raise UserError(
                "Findings without a verdict: " + ", ".join(unjudged)
            )
        task.status = ReviewStatus.FINISHED
        task.finished = now()
        store.save(task)
        remove_clone(task)
    print("Review finished.")


async def post_review(args):
    with open_review(args.state, args.lock_timeout) as store:
        task = store.load()
        task.require_status(ReviewStatus.FINISHED)
        if not isinstance(task.source, GithubSource):
            raise UserError("The review has no pull request.")
        if task.comment_url is not None:
            raise UserError(
                f"The review is already posted: {task.comment_url}"
            )
        if not confirmed(task.findings):
            print("No confirmed findings; nothing posted.")
            return
        await post_pr_comment(task, args.state)
        store.save(task)
    print(task.comment_url)


async def post_pr_comment(task: JevInlineReview, state: pathlib.Path):
    source = task.source
    pull_request = await github.gh.gh.pull_request(source.repo, source.pr)
    user = await github.gh.gh.current_user(source.repo.host)
    review = jev_inline_review(
        task.result(state),
        {
            path: [range(r.start, r.stop) for r in ranges]
            for path, ranges in task.github_hunks.items()
        },
        # GitHub refuses a request for changes on one's own pull request.
        github.pull_request.ReviewEvent.COMMENT
        if pull_request.author.login == user.login
        else github.pull_request.ReviewEvent.REQUEST_CHANGES,
    )
    url = await github.gh.gh.post_review(
        repo=source.repo, pr=source.pr, review=review
    )
    task.comment_url = str(url)


async def cancel_review(args):
    with open_review(args.state, args.lock_timeout) as store:
        task = store.load()
        task.require_status(ReviewStatus.OPEN)
        task.status = ReviewStatus.CANCELLED
        task.finished = now()
        store.save(task)
        remove_clone(task)
    print("Review cancelled.")


async def read_review(args):
    with open_review(args.state, args.lock_timeout) as store:
        task = store.load()
    task.require_status(ReviewStatus.FINISHED)
    print(jev_inline_text(task.result(args.state)))


async def sandbox_bash(args):
    command = sys.stdin.read()
    await in_sandbox(args, lambda sandbox: sandbox.run_bash(command))


async def sandbox_read(args):
    await in_sandbox(
        args,
        lambda sandbox: sandbox.read_lines(
            args.file_path, args.offset, args.limit, args.max_file_size
        ),
    )


async def in_sandbox(args, call):
    """Write what call returns for the open review's sandbox."""
    with open_review(args.state, args.lock_timeout) as store:
        task = store.load()
        task.require_status(ReviewStatus.OPEN)
        checkout = review_checkout(task)
        with (
            sandbox_home(task, store.read_file(JEV_FINDINGS)) as prepared,
            opened_sandbox(store, prepared, {task.mount: checkout}) as sandbox,
        ):
            output = call(sandbox)
    sys.stdout.write(output)


def task_brief(task):
    # What start-review prints: where the review's head is in the sandbox,
    # then its input as YAML.
    given = yaml.safe_dump(
        {"input": task.input.model_dump(mode="json", exclude_none=True)},
        allow_unicode=True,
        sort_keys=False,
    ).rstrip("\n")
    audited = "every file" if task.full else f"from {task.source.base_label()}"
    return f"{task.mount} (head, audited {audited})\n{given}"


async def run_jev_scan(root, rules, extra_rules, base=None, evaluate=None):
    """Run jev-scan on root against the rule sets under rules and each of
    extra_rules: its findings as JSON lines, and its summary line. Fails if
    any jev request fails."""
    try:
        async with asyncio.timeout(JEV_SCAN_TIMEOUT):
            root, scanned = await jev_pr_review.jev_scan.scan_items(
                rules,
                root=root,
                extra_rules=extra_rules,
                base=base,
                evaluate=evaluate,
            )
            found, failures = await jev_pr_review.jev_scan.review(
                scanned, root, JEV_SCAN_CONCURRENCY, verbose=False
            )
    except TimeoutError:
        raise UserError(f"jev-scan ran longer than {JEV_SCAN_TIMEOUT} seconds")
    except (
        jev_pr_review.jev_scan.UserError,
        jev_pr_review.jev_ask.UserError,
    ) as e:
        raise UserError(f"jev-scan: {e}")
    if failures:
        raise UserError(
            f"jev-scan: {len(failures)} jev requests failed:\n"
            + "\n".join(failures)
        )
    lines = [
        jev_pr_review.jev_scan.json_dumps(f.model_dump(exclude_none=True))
        for f in found
    ]
    summary = jev_pr_review.jev_scan.summary_line(
        scanned, root, found, failures
    )
    return "".join(f"{line}\n" for line in lines), summary


def at_mount(findings, checkout, mount):
    """Each line of jev-scan's findings, its file moved from the host
    checkout to where the sandbox mounts it."""
    for line in text_lines(findings):
        item = json.loads(line)
        if isinstance(file := item.get("file"), str):
            path = pathlib.PurePosixPath(file)
            if path.is_relative_to(checkout):
                item["file"] = str(mount / path.relative_to(checkout))
        yield json.dumps(item, sort_keys=True)


async def checkout_detached(checkout: pathlib.Path, commit):
    await git(
        checkout / ".git",
        f"--work-tree={checkout}",
        "checkout",
        "--quiet",
        "--detach",
        commit,
    )


async def pull_request_source(
    git_dir,
    repo: github.repo.GithubRepo,
    pr: github.pull_request.GithubPRNumber,
):
    head_ref = f"refs/pr/{pr}/head"
    target_ref = f"refs/pr/{pr}/target"
    target = (
        (await git(git_dir, "symbolic-ref", target_ref))
        .strip()
        .removeprefix("refs/remotes/origin/")
    )
    head_sha, target_sha = (
        await git(git_dir, "rev-parse", head_ref, target_ref)
    ).split()
    merge_base = (
        await git(git_dir, "merge-base", head_sha, target_sha)
    ).strip()
    return GithubSource(
        repo=repo,
        pr=pr,
        head_sha=head_sha,
        target=target,
        target_sha=target_sha,
        merge_base=merge_base,
    )


async def prepare_github_checkout(
    root: pathlib.Path,
    repo: github.repo.GithubRepo,
    pr: github.pull_request.GithubPRNumber,
) -> tuple[GithubSource, pathlib.Path]:
    target = (await github.gh.gh.pull_request(repo, pr)).base_ref_name
    checkout = root / HEAD
    clone = checkout / ".git"
    await github.gh.gh.clone(repo, checkout, "--no-checkout")
    await git(
        clone,
        "fetch",
        "--quiet",
        "origin",
        f"+refs/pull/{pr}/head:refs/pr/{pr}/head",
    )
    await git(
        clone,
        "symbolic-ref",
        f"refs/pr/{pr}/target",
        f"refs/remotes/origin/{target}",
    )
    source = await pull_request_source(clone, repo, pr)
    source.clone = clone
    await checkout_detached(checkout, source.head_sha)
    return source, checkout


async def prepare_local_checkout(
    root: pathlib.Path,
    git_dir: pathlib.Path,
    base: str,
    head: str,
) -> tuple[LocalSource, pathlib.Path]:
    git_dir = git_dir.resolve()
    base_commit, head_commit = [
        await resolve_commit(git_dir, rev) for rev in (base, head)
    ]
    checkout = root / HEAD
    clone = checkout / ".git"
    await run(
        "git", "clone", "--quiet", "--no-checkout", "--", git_dir, checkout
    )
    await checkout_detached(checkout, head_commit)
    source = LocalSource(
        git_dir=git_dir,
        clone=clone,
        base_rev=base,
        head_rev=head,
        base_commit=base_commit,
        head_commit=head_commit,
    )
    return source, checkout


async def resolve_commit(git_dir: pathlib.Path, rev: str) -> str:
    return (
        await git(
            git_dir,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{rev}^{{commit}}",
        )
    ).strip()


class Template(enum.StrEnum):
    JEV_INLINE_REVIEW = "jev_inline_review.md"
    JEV_INLINE = "jev_inline.md"
    JEV_INLINE_COMMENT = "jev_inline_comment.md"


# keep_trailing_newline: every comment's trailer ends with one.
TEMPLATES = minijinja.Environment(
    keep_trailing_newline=True,
    templates={
        Template.JEV_INLINE_REVIEW: jev_pr_review.templates.JEV_INLINE_REVIEW,
        Template.JEV_INLINE: jev_pr_review.templates.JEV_INLINE,
        Template.JEV_INLINE_COMMENT: (
            jev_pr_review.templates.JEV_INLINE_COMMENT
        ),
    },
)


def render(template: Template, model: pydantic.BaseModel) -> str:
    return TEMPLATES.render_template(template, model=model)


def inline_finding(
    finding: jev_pr_review.jev_scan.Finding, checkout: pathlib.Path
) -> JevInlineFinding:
    return JevInlineFinding(
        path=str(pathlib.Path(finding.file).relative_to(checkout)),
        start=finding.start,
        end=finding.end,
        evaluated=finding.evaluated,
        rule=finding.rule,
        flags=finding.flags,
    )


def confirmed(findings: list[JevInlineFinding]) -> list[JevInlineFinding]:
    return [f for f in findings if f.verdict == JevVerdict.CONFIRMED]


def jev_inline_front_matter(
    result: JevInlineReviewResult, exclude=frozenset()
) -> str:
    front = {
        **result.source.model_dump(mode="json"),
        **result.model_dump(
            mode="json",
            exclude={"source", "findings", *exclude},
            exclude_none=True,
        ),
        "findings": len(result.findings),
        "confirmed": len(confirmed(result.findings)),
        "rejected": sum(
            f.verdict == JevVerdict.REJECTED for f in result.findings
        ),
    }
    return yaml.safe_dump(front, allow_unicode=True, sort_keys=True).rstrip(
        "\n"
    )


def jev_inline_rules(
    findings: list[JevInlineFinding],
) -> list["JevReportRule"]:
    by_rule: dict[str, list[JevInlineFinding]] = {}
    for f in sorted(confirmed(findings), key=lambda f: (f.path, f.start)):
        by_rule.setdefault(f.flags, []).append(f)
    return [
        JevReportRule(flags=flags, findings=found)
        for flags, found in by_rule.items()
    ]


def jev_inline_text(result: JevInlineReviewResult) -> str:
    return render(
        Template.JEV_INLINE,
        JevInlineText(
            front_matter=jev_inline_front_matter(result),
            rules=jev_inline_rules(result.findings),
        ),
    )


def jev_inline_comment(f: JevInlineFinding) -> str:
    return render(
        Template.JEV_INLINE_COMMENT,
        JevInlineComment(reason=f.reason or "", flags=f.flags),
    )


# Lines of context around each change in a pull request's diff on GitHub,
# and the most lines between two changes it shows as one hunk.
GITHUB_DIFF_CONTEXT = 3
GITHUB_INTER_HUNK_CONTEXT = 1


async def github_hunks(source: GithubSource) -> dict[str, list[range]]:
    """The head's lines of each hunk of the pull request's diff as GitHub
    shows it, by path: the lines a review comment can be left on."""
    diff = await git(
        source.clone,
        "diff",
        "--no-color",
        "--no-ext-diff",
        f"-U{GITHUB_DIFF_CONTEXT}",
        f"--inter-hunk-context={GITHUB_INTER_HUNK_CONTEXT}",
        source.merge_base,
        source.head_sha,
    )
    return {
        file.path: [
            range(hunk.target_start, hunk.target_start + hunk.target_length)
            for hunk in file
            if hunk.target_length
        ]
        for file in unidiff.PatchSet(diff)
    }


def jev_review_comment(
    f: JevInlineFinding, hunks: dict[str, list[range]]
) -> github.pull_request.ReviewComment:
    """A comment on f's lines within the first GitHub hunk they overlap, or
    on its file when f is a file finding or overlaps none."""
    body = jev_inline_comment(f)
    if f.evaluated != jev_pr_review.jev_scan.Evaluation.FILE:
        for hunk in hunks.get(f.path, []):
            start, end = max(f.start, hunk.start), min(f.end, hunk.stop - 1)
            if start <= end:
                return github.pull_request.LineReviewComment(
                    path=f.path,
                    body=body,
                    line=end,
                    start_line=start if start < end else None,
                )
    return github.pull_request.FileReviewComment(path=f.path, body=body)


# The most characters GitHub takes in a review's body.
GITHUB_BODY_LIMIT = 65536


class JevOutsideRule(pydantic.BaseModel):
    """A rule's findings in files the pull request did not change."""

    flags: str
    findings: list[JevInlineFinding]


class JevInlineReviewBody(pydantic.BaseModel):
    """The input of the jev inline review body's template."""

    found: int
    # None when no finding is in a file the pull request did not change.
    outside: list[JevOutsideRule] | None = None
    left_out: int = 0


class JevReportRule(JevOutsideRule):
    """A rule's confirmed findings in a jev inline review's report."""


class JevInlineText(pydantic.BaseModel):
    """The input of the jev inline review result's template."""

    front_matter: str
    rules: list[JevReportRule]


class JevInlineComment(pydantic.BaseModel):
    """The input of the template of a jev inline review's comment."""

    reason: str
    flags: str


def jev_inline_review_body(
    found: list[JevInlineFinding], outside: list[JevInlineFinding]
) -> str:
    """The body of the review of found, whose findings in outside, which
    are in files the pull request did not change, are listed grouped by
    rule in as many as GitHub's limit has room for."""
    body = JevInlineReviewBody(found=len(found))
    if not outside:
        return render(Template.JEV_INLINE_REVIEW, body)

    def size(rules: list[JevOutsideRule], left_out: int = 0) -> int:
        update = {"outside": rules, "left_out": left_out}
        return len(
            render(
                Template.JEV_INLINE_REVIEW,
                body.model_copy(update=update),
            )
        )

    base = size([])
    used = base
    # Room for the longest note of findings left out.
    room = GITHUB_BODY_LIMIT - (size([], len(outside)) - base)
    by_flags: dict[str, list[JevInlineFinding]] = {}
    for f in sorted(outside, key=lambda f: (f.path, f.start)):
        by_flags.setdefault(f.flags, []).append(f)
    rules: list[JevOutsideRule] = []
    left_out = 0
    for flags, group in by_flags.items():
        empty = size([JevOutsideRule(flags=flags, findings=[])])
        heading = empty - base
        kept: list[JevInlineFinding] = []
        for f in group:
            item = size([JevOutsideRule(flags=flags, findings=[f])]) - empty
            if used + heading + item > room:
                left_out += 1
            else:
                kept.append(f)
                used += item
        if kept:
            rules.append(JevOutsideRule(flags=flags, findings=kept))
            used += heading
    return render(
        Template.JEV_INLINE_REVIEW,
        body.model_copy(update={"outside": rules, "left_out": left_out}),
    )


def jev_inline_review(
    result: JevInlineReviewResult,
    hunks: dict[str, list[range]],
    event: github.pull_request.ReviewEvent,
) -> github.pull_request.NewReview:
    found = confirmed(result.findings)
    # GitHub refuses a review comment on a file the pull request does not
    # change, which a full review's findings can be in.
    inside = [f for f in found if f.path in hunks]
    outside = [f for f in found if f.path not in hunks]
    return github.pull_request.NewReview(
        commit_id=result.source.head_sha,
        event=event,
        body=jev_inline_review_body(found, outside),
        comments=[jev_review_comment(f, hunks) for f in inside],
    )


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


def parser():
    parser = argparse.ArgumentParser(
        prog="jev-code-review",
        description="Run jev over a pull request or a local git dir, judge "
        "its findings, and post the confirmed ones as a review.",
    )
    parser.add_argument(
        "--state",
        type=pathlib.Path,
        required=True,
        metavar="FILE",
        help="SQLite file holding all of one review: its source, clone and "
        "head, sandbox, findings, verdicts, and result",
    )
    parser.add_argument(
        "--lock-timeout",
        type=positive_float,
        default=3600,
        metavar="SECONDS",
        help="Seconds to wait for another command to release the state file "
        "[default: 3600]",
    )
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )

    def command(name, handler, help):
        sub = commands.add_parser(name, help=help, description=help)
        sub.set_defaults(handler=handler)
        return sub

    start = command(
        "start-review",
        start_review,
        "Prepare a jev review of a pull request or a local git dir in a new "
        "state file, run jev over its change, or every file of its head when "
        "full, and print its brief and the scan's summary",
    )
    start.add_argument(
        "--repo",
        metavar="HOST/ORG/NAME",
        help="GitHub repo of the pull request; requires --pr; conflicts with "
        "--git-dir",
    )
    start.add_argument(
        "--pr",
        type=positive_int,
        metavar="N",
        help="Pull request number; requires --repo",
    )
    start.add_argument(
        "--cache-dir",
        type=pathlib.Path,
        metavar="DIR",
        help="Directory the review's clone is kept in until the review "
        "finishes or is cancelled [default: the temporary directory]",
    )
    start.add_argument(
        "--git-dir",
        metavar="DIR",
        help="Git dir on the host; requires --head; conflicts with --repo",
    )
    start.add_argument(
        "--base",
        metavar="REV",
        help="Revision to compare from; requires --git-dir; conflicts with "
        "--full",
    )
    start.add_argument(
        "--head",
        metavar="REV",
        help="Revision to audit; requires --git-dir",
    )
    start.add_argument(
        "--rules",
        type=pathlib.Path,
        default=jev_pr_review.jev_scan.env_path("JEV_RULES_DIR"),
        metavar="DIR",
        help="Directory of rule sets jev-scan reviews against "
        "[default: $JEV_RULES_DIR]; required",
    )
    start.add_argument(
        "--extra-rules",
        type=jev_pr_review.jev_scan.path_list,
        default=jev_pr_review.jev_scan.path_list(
            os.environ.get("JEV_RULES_EXTRA", "")
        ),
        metavar="DIRS",
        help=f"More directories of rule sets, separated by {os.pathsep!r} "
        "[default: $JEV_RULES_EXTRA]",
    )
    start.add_argument(
        "--full",
        action="store_true",
        help="Audit every file of the head, not the change from the base",
    )
    for name, handler, help, reason in (
        (
            "confirm-finding",
            confirm_finding,
            "Confirm one jev_scan finding of the review",
            "One sentence on how the lines match the violation",
        ),
        (
            "reject-finding",
            reject_finding,
            "Reject one jev_scan finding of the review",
            "One sentence on why the lines do not break the rule",
        ),
    ):
        judge = command(name, handler, help)
        judge.add_argument(
            "index",
            type=positive_int,
            metavar="INDEX",
            help="Index of the finding in jev_scan's file, from 1",
        )
        judge.add_argument("reason", metavar="REASON", help=reason)
    command(
        "finish-review",
        finish_review,
        "Finish the review once every finding has a verdict",
    )
    command(
        "post-review",
        post_review,
        "Post the finished review's confirmed findings as a review on its "
        "pull request",
    )
    command(
        "cancel-review",
        cancel_review,
        "Mark the review cancelled, so no later command changes it",
    )
    command("read-review", read_review, "Print the finished review's result")
    sandbox = commands.add_parser(
        "sandbox",
        help="Run a bashkit-sandbox command in the review's sandbox",
        description="Run a bashkit-sandbox command in the review's sandbox",
    ).add_subparsers(dest="sandbox_command", required=True, metavar="COMMAND")
    bash = sandbox.add_parser(
        "bash",
        help="Run the shell command read from stdin",
        description="Run the shell command read from stdin",
    )
    bash.set_defaults(handler=sandbox_bash)
    read = sandbox.add_parser(
        "read",
        help="Read a text file with numbered lines",
        description="Read a text file with numbered lines",
    )
    read.set_defaults(handler=sandbox_read)
    read.add_argument(
        "--file-path",
        type=pathlib.PurePosixPath,
        required=True,
        metavar="PATH",
        help="The file to read",
    )
    read.add_argument(
        "--offset",
        type=positive_int,
        metavar="N",
        help="The first line to read, from 1",
    )
    read.add_argument(
        "--limit",
        type=non_negative_int,
        metavar="N",
        help="The number of lines to read",
    )
    read.add_argument(
        "--max-file-size",
        type=positive_int,
        default=16 * 1024 * 1024,
        metavar="BYTES",
        help="The largest file, in bytes, to read a range of lines from "
        "[default: 16777216]",
    )
    return parser


def cli():
    args = parser().parse_args()
    try:
        sys.exit(asyncio.run(args.handler(args)) or 0)
    except (
        UserError,
        jev_pr_review.sandbox.UserError,
        ProcessError,
        github.gh.GithubError,
        sqlite3.OperationalError,
        # bashkit raises RuntimeError for its own errors, such as a
        # read-only filesystem.
        RuntimeError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()

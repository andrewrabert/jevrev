"""Run jev over a directory or a GitHub pull request into a review's db, confirm or reject each of
its findings, and print the review or post it on the pull request once every finding has a
verdict."""

import argparse
import asyncio
import contextlib
import enum
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

import minijinja
import pydantic
import unidiff
import yaml

import jevrev.github.gh
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.jev_ask
import jevrev.jev_scan
import jevrev.templates

# Seconds a jev scan may run.
JEV_SCAN_TIMEOUT = 600
# Requests to jev a scan has open at once.
JEV_SCAN_CONCURRENCY = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY CHECK (id = 0),
    data TEXT NOT NULL
);
"""

# Lines of context around each change in a pull request's diff on GitHub,
# and the most lines between two changes it shows as one hunk.
GITHUB_DIFF_CONTEXT = 3
GITHUB_INTER_HUNK_CONTEXT = 1
# The most characters GitHub takes in a review's body.
GITHUB_BODY_LIMIT = 65536


class UserError(Exception):
    pass


class Verdict(enum.StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Finding(jevrev.jev_scan.Finding):
    """A jev_scan finding, its file relative to the review's dir, with its
    index in the scan's findings, from 1, and its verdict once it has one."""

    index: int
    verdict: Verdict | None = None
    reason: str | None = None


class PullRequestSource(pydantic.BaseModel):
    """The pull request a review's dir is a clone of, at its head."""

    repo: jevrev.github.repo.GithubRepo
    pr: jevrev.github.pull_request.GithubPRNumber
    head_sha: jevrev.github.pull_request.GitSha
    target: str
    target_sha: jevrev.github.pull_request.GitSha
    merge_base: jevrev.github.pull_request.GitSha


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
    metadata: pydantic.JsonValue = None
    # jev_scan's findings, in its order.
    findings: list[Finding]


class Format(enum.StrEnum):
    JSON = "json"
    MD = "md"


class Store:
    """A review in its db."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(SCHEMA)

    def load(self) -> Review:
        row = self.db.execute("SELECT data FROM review").fetchone()
        if row is None:
            raise UserError("The db holds no review.")
        return Review.model_validate_json(row[0])

    def save(self, review: Review):
        self.db.execute(
            "INSERT OR REPLACE INTO review (id, data) VALUES (0, ?)",
            (review.model_dump_json(),),
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


@contextlib.contextmanager
def open_review(path: pathlib.Path, lock_timeout: float):
    if not path.is_file():
        raise UserError(f"{path}: no such db file")
    with open_db(path, lock_timeout) as store:
        yield store


def judged_review(args) -> Review:
    """The review in the db. Fails while any finding has no verdict."""
    with open_review(args.db, args.lock_timeout) as store:
        review = store.load()
    if unjudged := [
        str(finding.index)
        for finding in review.findings
        if finding.verdict is None
    ]:
        raise UserError("Findings without a verdict: " + ", ".join(unjudged))
    return review


async def git(root: pathlib.Path, *argv) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "--no-pager",
        "-C",
        str(root),
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise UserError(f"git {argv[0]}: {stderr.decode().strip()}")
    return stdout.decode()


async def clone_pull_request(
    repo: jevrev.github.repo.GithubRepo,
    pr: jevrev.github.pull_request.GithubPRNumber,
    dest: pathlib.Path,
) -> PullRequestSource:
    """Clone the pull request to dest, its head checked out."""
    target = (await jevrev.github.gh.gh.pull_request(repo, pr)).base_ref_name
    await jevrev.github.gh.gh.clone(repo, dest, "--no-checkout")
    await git(
        dest,
        "fetch",
        "--quiet",
        "origin",
        f"+refs/pull/{pr}/head:refs/pr/{pr}/head",
    )
    head_sha, target_sha = (
        await git(
            dest,
            "rev-parse",
            f"refs/pr/{pr}/head",
            f"refs/remotes/origin/{target}",
        )
    ).split()
    merge_base = (await git(dest, "merge-base", head_sha, target_sha)).strip()
    await git(dest, "checkout", "--quiet", "--detach", head_sha)
    return PullRequestSource(
        repo=repo,
        pr=pr,
        head_sha=head_sha,
        target=target,
        target_sha=target_sha,
        merge_base=merge_base,
    )


async def cmd_scan(args):
    db: pathlib.Path = args.db
    if db.exists():
        raise UserError(f"{db} already exists")
    if args.rules is None:
        raise UserError("Give --rules or set JEV_RULES_DIR.")
    pull_request = None
    if args.repo is not None:
        if args.pr is None:
            raise UserError("--repo requires --pr.")
        if args.base is not None:
            raise UserError("--base requires --dir.")
        if args.cache_dir is not None:
            args.cache_dir.mkdir(parents=True, exist_ok=True)
        root = pathlib.Path(
            tempfile.mkdtemp(prefix="jevrev-", dir=args.cache_dir)
        ).resolve()
        pull_request = await clone_pull_request(args.repo, args.pr, root)
        base = None if args.full else pull_request.merge_base
    else:
        if args.pr is not None:
            raise UserError("--pr requires --repo.")
        if (args.base is None) != args.full:
            raise UserError("--dir requires one of --base or --full.")
        root = args.dir.resolve()
        base = args.base
    evaluate = None
    if base is not None:
        evaluate = [
            jevrev.jev_scan.Evaluation.HUNK,
            jevrev.jev_scan.Evaluation.FILE,
        ]
    found, summary = await run_jev_scan(
        root=root,
        rules=args.rules,
        extra_rules=args.extra_rules,
        base=base,
        evaluate=evaluate,
    )
    review = Review(
        dir=root,
        base=base,
        full=args.full,
        pull_request=pull_request,
        metadata=args.metadata,
        findings=[
            Finding(
                **finding.model_dump(exclude={"file"}),
                file=str(pathlib.Path(finding.file).relative_to(root)),
                index=index,
            )
            for index, finding in enumerate(found, 1)
        ],
    )
    try:
        with open_db(db, args.lock_timeout) as store:
            store.save(review)
    except BaseException:
        db.unlink(missing_ok=True)
        raise
    print(summary)


async def run_jev_scan(root, rules, extra_rules, base=None, evaluate=None):
    """Run jev-scan on root against the rule sets under rules and each of
    extra_rules: its findings, and its summary line. Fails if any jev
    request fails."""
    try:
        async with asyncio.timeout(JEV_SCAN_TIMEOUT):
            root, scanned = await jevrev.jev_scan.scan_items(
                rules,
                root=root,
                extra_rules=extra_rules,
                base=base,
                evaluate=evaluate,
            )
            found, failures = await jevrev.jev_scan.review(
                scanned=scanned,
                root=root,
                concurrency=JEV_SCAN_CONCURRENCY,
                verbose=False,
            )
    except TimeoutError:
        raise UserError(f"jev-scan ran longer than {JEV_SCAN_TIMEOUT} seconds")
    except (
        jevrev.jev_scan.UserError,
        jevrev.jev_ask.UserError,
    ) as e:
        raise UserError(f"jev-scan: {e}")
    if failures:
        raise UserError(
            f"jev-scan: {len(failures)} jev requests failed:\n"
            + "\n".join(failures)
        )
    summary = jevrev.jev_scan.summary_line(
        scanned=scanned, root=root, found=found, failures=failures
    )
    return found, summary


async def cmd_pending(args):
    with open_review(args.db, args.lock_timeout) as store:
        review = store.load()
    sys.stdout.write(
        "".join(
            jevrev.jev_scan.json_dumps(
                finding.model_dump(
                    mode="json",
                    exclude={"verdict", "reason"},
                    exclude_none=True,
                )
            )
            + "\n"
            for finding in review.findings
            if finding.verdict is None
        )
    )


async def cmd_confirm(args):
    await judge(args, Verdict.CONFIRMED)


async def cmd_reject(args):
    await judge(args, Verdict.REJECTED)


async def judge(args, verdict: Verdict):
    index = args.index
    with open_review(args.db, args.lock_timeout) as store:
        review = store.load()
        count = len(review.findings)
        if not 1 <= index <= count:
            raise UserError(f"index: {index} is not in 1..{count}")
        finding = review.findings[index - 1]
        if finding.verdict is not None:
            raise UserError(
                f"index: finding {index} is already {finding.verdict}"
            )
        finding.verdict = verdict
        finding.reason = args.reason
        store.save(review)
    print(f"Finding {index} {verdict}")


async def cmd_show(args):
    review = judged_review(args)
    match args.format:
        case Format.JSON:
            print(review.model_dump_json())
        case Format.MD:
            print(review_text(review))


async def cmd_post(args):
    review = judged_review(args)
    source = review.pull_request
    if source is None:
        raise UserError("The review is not of a pull request.")
    if not confirmed(review.findings):
        print("No confirmed findings; no GitHub review submitted.")
        return
    gh = jevrev.github.gh.gh
    pull_request = await gh.pull_request(source.repo, source.pr)
    user = await gh.current_user(source.repo.host)
    url = await gh.post_review(
        repo=source.repo,
        pr=source.pr,
        review=pr_review(
            source=source,
            findings=review.findings,
            hunks=await github_hunks(review.dir, source),
            # GitHub refuses a request for changes on one's own pull
            # request.
            event=jevrev.github.pull_request.ReviewEvent.COMMENT
            if pull_request.author.login == user.login
            else jevrev.github.pull_request.ReviewEvent.REQUEST_CHANGES,
        ),
    )
    print(url)


class Template(enum.StrEnum):
    REVIEW = "review.md"
    PR_REVIEW = "pr_review.md"
    PR_COMMENT = "pr_comment.md"


# keep_trailing_newline: every comment's trailer ends with one.
TEMPLATES = minijinja.Environment(
    keep_trailing_newline=True,
    templates={
        Template.REVIEW: jevrev.templates.REVIEW,
        Template.PR_REVIEW: jevrev.templates.PR_REVIEW,
        Template.PR_COMMENT: jevrev.templates.PR_COMMENT,
    },
)


def render(template: Template, model: pydantic.BaseModel) -> str:
    return TEMPLATES.render_template(template, model=model)


class ReportRule(pydantic.BaseModel):
    """A rule's confirmed findings."""

    flags: str
    findings: list[Finding]


class ReviewText(pydantic.BaseModel):
    """The input of the review markdown's template."""

    front_matter: str
    rules: list[ReportRule]


class PrReviewBody(pydantic.BaseModel):
    """The input of the pull request review body's template."""

    found: int
    # None when no finding is in a file the pull request did not change.
    outside: list[ReportRule] | None = None
    left_out: int = 0


class PrComment(pydantic.BaseModel):
    """The input of the template of a pull request review's comment."""

    reason: str
    flags: str


def confirmed(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.verdict == Verdict.CONFIRMED]


def by_rule(findings: list[Finding]) -> dict[str, list[Finding]]:
    """findings grouped by rule, each group in order of file and line."""
    grouped: dict[str, list[Finding]] = {}
    for f in sorted(findings, key=lambda f: (f.file, f.start)):
        grouped.setdefault(f.flags, []).append(f)
    return grouped


def front_matter(review: Review) -> str:
    front = {
        **review.model_dump(
            mode="json", exclude={"findings"}, exclude_none=True
        ),
        "findings": len(review.findings),
        "confirmed": len(confirmed(review.findings)),
        "rejected": sum(
            f.verdict == Verdict.REJECTED for f in review.findings
        ),
    }
    return yaml.safe_dump(front, allow_unicode=True, sort_keys=True).rstrip(
        "\n"
    )


def review_text(review: Review) -> str:
    return render(
        Template.REVIEW,
        ReviewText(
            front_matter=front_matter(review),
            rules=[
                ReportRule(flags=flags, findings=found)
                for flags, found in by_rule(confirmed(review.findings)).items()
            ],
        ),
    )


async def github_hunks(
    root: pathlib.Path, source: PullRequestSource
) -> dict[str, list[range]]:
    """The head's lines of each hunk of the pull request's diff as GitHub
    shows it, by path: the lines a review comment can be left on."""
    diff = await git(
        root,
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


def pr_comment(
    f: Finding, hunks: dict[str, list[range]]
) -> jevrev.github.pull_request.ReviewComment:
    """A comment on f's lines within the first GitHub hunk they overlap, or
    on its file when f is a file finding or overlaps none."""
    body = render(
        Template.PR_COMMENT, PrComment(reason=f.reason or "", flags=f.flags)
    )
    if f.evaluated != jevrev.jev_scan.Evaluation.FILE:
        for hunk in hunks.get(f.file, []):
            start, end = max(f.start, hunk.start), min(f.end, hunk.stop - 1)
            if start <= end:
                return jevrev.github.pull_request.LineReviewComment(
                    path=f.file,
                    body=body,
                    line=end,
                    start_line=start if start < end else None,
                )
    return jevrev.github.pull_request.FileReviewComment(path=f.file, body=body)


def pr_review_body(found: list[Finding], outside: list[Finding]) -> str:
    """The body of the review of found, whose findings in outside, which
    are in files the pull request did not change, are listed grouped by
    rule in as many as GitHub's limit has room for."""
    body = PrReviewBody(found=len(found))
    if not outside:
        return render(Template.PR_REVIEW, body)

    def size(rules: list[ReportRule], left_out: int = 0) -> int:
        update = {"outside": rules, "left_out": left_out}
        return len(render(Template.PR_REVIEW, body.model_copy(update=update)))

    base = size([])
    used = base
    # Room for the longest note of findings left out.
    room = GITHUB_BODY_LIMIT - (size([], len(outside)) - base)
    rules: list[ReportRule] = []
    left_out = 0
    for flags, group in by_rule(outside).items():
        empty = size([ReportRule(flags=flags, findings=[])])
        heading = empty - base
        kept: list[Finding] = []
        for f in group:
            item = size([ReportRule(flags=flags, findings=[f])]) - empty
            if used + heading + item > room:
                left_out += 1
            else:
                kept.append(f)
                used += item
        if kept:
            rules.append(ReportRule(flags=flags, findings=kept))
            used += heading
    return render(
        Template.PR_REVIEW,
        body.model_copy(update={"outside": rules, "left_out": left_out}),
    )


def pr_review(
    source: PullRequestSource,
    findings: list[Finding],
    hunks: dict[str, list[range]],
    event: jevrev.github.pull_request.ReviewEvent,
) -> jevrev.github.pull_request.NewReview:
    found = confirmed(findings)
    # GitHub refuses a review comment on a file the pull request does not
    # change, which a full review's findings can be in.
    inside = [f for f in found if f.file in hunks]
    outside = [f for f in found if f.file not in hunks]
    return jevrev.github.pull_request.NewReview(
        commit_id=source.head_sha,
        event=event,
        body=pr_review_body(found, outside),
        comments=[pr_comment(f, hunks) for f in inside],
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


def json_value(value):
    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"{value}: {e}") from e


def github_repo(value) -> jevrev.github.repo.GithubRepo:
    host, *rest = value.split("/")
    if len(rest) != 2:
        raise argparse.ArgumentTypeError(f"{value}: expected HOST/ORG/NAME")
    try:
        return jevrev.github.repo.GithubRepo(
            host=host, org=rest[0], name=rest[1]
        )
    except pydantic.ValidationError as e:
        problems = "; ".join(
            problem["msg"] for problem in e.errors(include_url=False)
        )
        raise argparse.ArgumentTypeError(f"{value}: {problems}") from e


def add_judge_arguments(parser, reason_help):
    parser.add_argument(
        "index",
        type=positive_int,
        metavar="INDEX",
        help="Index of the finding in the scan's findings, from 1",
    )
    parser.add_argument("reason", metavar="REASON", help=reason_help)


def parser():
    parser = argparse.ArgumentParser(
        prog="jevrev",
        description="Run jev over a directory or a GitHub pull request, "
        "confirm or reject each of its findings, and print the review or "
        "post it on the pull request.",
    )
    parser.add_argument(
        "--db",
        type=pathlib.Path,
        required=True,
        metavar="FILE",
        help="SQLite file holding one review: its dir, base, pull request, "
        "metadata, findings, and verdicts",
    )
    parser.add_argument(
        "--lock-timeout",
        type=positive_float,
        default=3600,
        metavar="SECONDS",
        help="Seconds to wait for another command to release the db "
        "[default: 3600]",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )

    description = (
        "Run jev over a directory, or a clone of a pull request, into a new "
        "db: the change from a base, or every file when full; print the "
        "scan's summary"
    )
    scan_parser = subparsers.add_parser(
        "scan", help=description, description=description
    )
    source_group = scan_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--dir",
        type=pathlib.Path,
        metavar="DIR",
        help="Directory to scan in place; findings are relative to it; "
        "requires --base or --full; conflicts with --repo",
    )
    source_group.add_argument(
        "--repo",
        type=github_repo,
        metavar="HOST/ORG/NAME",
        help="GitHub repo of the pull request to clone and scan; requires "
        "--pr; conflicts with --dir",
    )
    scan_parser.add_argument(
        "--pr",
        type=positive_int,
        metavar="N",
        help="Pull request number; requires --repo",
    )
    scan_parser.add_argument(
        "--cache-dir",
        type=pathlib.Path,
        metavar="DIR",
        help="Directory the pull request is cloned under, and left in "
        "[default: the temporary directory]",
    )
    scan_parser.add_argument(
        "--base",
        metavar="REV",
        help="Revision of the git dir to compare from; requires --dir; "
        "conflicts with --full",
    )
    scan_parser.add_argument(
        "--full",
        action="store_true",
        help="Scan every file, not the change from the base or the pull "
        "request's merge base",
    )
    scan_parser.add_argument(
        "--rules",
        type=pathlib.Path,
        default=jevrev.jev_scan.env_path("JEV_RULES_DIR"),
        metavar="DIR",
        help="Directory of rule sets jev-scan reviews against "
        "[default: $JEV_RULES_DIR]; required",
    )
    scan_parser.add_argument(
        "--extra-rules",
        type=jevrev.jev_scan.path_list,
        default=jevrev.jev_scan.path_list(
            os.environ.get("JEV_RULES_EXTRA", "")
        ),
        metavar="DIRS",
        help=f"More directories of rule sets, separated by {os.pathsep!r} "
        "[default: $JEV_RULES_EXTRA]",
    )
    scan_parser.add_argument(
        "--metadata",
        type=json_value,
        metavar="JSON",
        help="JSON value stored with the review and printed by show",
    )

    description = (
        "Print each finding without a verdict as a line of JSON, its file "
        "relative to the dir"
    )
    subparsers.add_parser("pending", help=description, description=description)

    description = "Confirm one finding without a verdict"
    confirm_parser = subparsers.add_parser(
        "confirm", help=description, description=description
    )
    add_judge_arguments(
        confirm_parser, "One sentence on how the lines match the violation"
    )

    description = "Reject one finding without a verdict"
    reject_parser = subparsers.add_parser(
        "reject", help=description, description=description
    )
    add_judge_arguments(
        reject_parser, "One sentence on why the lines do not break the rule"
    )

    description = (
        "Print the review once every finding has a verdict; fails while any "
        "finding has none"
    )
    show_parser = subparsers.add_parser(
        "show", help=description, description=description
    )
    show_parser.add_argument(
        "--format",
        type=Format,
        choices=list(Format),
        default=Format.JSON,
        help="Print the review as JSON, or as markdown of its confirmed "
        "findings [default: json]",
    )

    description = (
        "Post the confirmed findings as a review on the pull request and "
        "print its URL, once every finding has a verdict; fails while any "
        "finding has none"
    )
    subparsers.add_parser("post", help=description, description=description)
    return parser


async def main():
    args = parser().parse_args()
    match args.command:
        case "scan":
            await cmd_scan(args)
        case "pending":
            await cmd_pending(args)
        case "confirm":
            await cmd_confirm(args)
        case "reject":
            await cmd_reject(args)
        case "show":
            await cmd_show(args)
        case "post":
            await cmd_post(args)


def cli():
    try:
        sys.exit(asyncio.run(main()))
    except (
        UserError,
        jevrev.github.gh.GithubError,
        sqlite3.OperationalError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()

"""Run jev over a directory or a GitHub pull request into a review's db, confirm or reject each of
its findings, and print the review or post it on the pull request once every finding has a
verdict."""

import argparse
import asyncio
import collections.abc
import contextlib
import enum
import os
import pathlib
import sys
import tempfile

import minijinja
import pydantic
import unidiff
import yaml

import jevrev.db
import jevrev.github.gh
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.jev.api
import jevrev.jev.ask
import jevrev.jev.scan
import jevrev.process
import jevrev.settings
import jevrev.state
import jevrev.templates

# Seconds a jev scan may run.
_JEV_SCAN_TIMEOUT = 600
# Requests to jev a scan has open at once.
_JEV_SCAN_CONCURRENCY = 8

# Lines of context around each change in a pull request's diff on GitHub,
# and the most lines between two changes it shows as one hunk.
_GITHUB_DIFF_CONTEXT = 3
_GITHUB_INTER_HUNK_CONTEXT = 1
# The most characters GitHub takes in a review's body.
_GITHUB_BODY_LIMIT = 65536


class UserError(Exception):
    pass


class Format(enum.StrEnum):
    JSON = "json"
    MD = "md"


def _load_review(store: jevrev.db.Store) -> jevrev.db.Review:
    review = store.load()
    if review is None:
        raise UserError("The db holds no review.")
    return review


@contextlib.contextmanager
def _open_review(path: pathlib.Path, lock_timeout: float):
    if not path.is_file():
        raise UserError(f"{path}: no such db file")
    with jevrev.db.open_db(path, lock_timeout) as store:
        yield store


def _judged_review(args) -> jevrev.db.Review:
    """The review in the db. Fails while any finding has no verdict."""
    with _open_review(args.db, args.lock_timeout) as store:
        review = _load_review(store)
    if unjudged := [
        str(finding.index)
        for finding in review.findings
        if finding.verdict is None
    ]:
        raise UserError("Findings without a verdict: " + ", ".join(unjudged))
    return review


class git:
    @staticmethod
    async def _run(root: pathlib.Path, argv: list[str]) -> str:
        """The stdout of git, in root, with argv."""
        try:
            stdout = await jevrev.process.run(
                ["git", "--no-pager", "-C", str(root), *argv]
            )
        except jevrev.process.ProcessError as e:
            raise UserError(str(e)) from e
        return stdout.decode()

    @staticmethod
    async def fetch(root: pathlib.Path, refspec: str):
        """Fetch refspec from origin."""
        await git._run(root, ["fetch", "--quiet", "origin", refspec])

    @staticmethod
    async def rev_parse(
        root: pathlib.Path, revisions: collections.abc.Sequence[str]
    ) -> list[str]:
        """The commit each of revisions names, in order."""
        return (await git._run(root, ["rev-parse", *revisions])).split()

    @staticmethod
    async def merge_base(root: pathlib.Path, first: str, second: str) -> str:
        return (await git._run(root, ["merge-base", first, second])).strip()

    @staticmethod
    async def checkout_detached(root: pathlib.Path, revision: str):
        await git._run(root, ["checkout", "--quiet", "--detach", revision])

    @staticmethod
    async def diff(
        root: pathlib.Path,
        base: str,
        head: str,
        context: int,
        inter_hunk_context: int,
    ) -> str:
        """The diff from base to head, with context lines around each change,
        and changes at most inter_hunk_context lines apart in one hunk."""
        return await git._run(
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


async def _clone_pull_request(
    repo: jevrev.github.repo.GithubRepo,
    pr: jevrev.github.pull_request.GithubPRNumber,
    dest: pathlib.Path,
) -> jevrev.db.PullRequestSource:
    """Clone the pull request to dest, its head checked out."""
    target = (await jevrev.github.gh.gh.pull_request(repo, pr)).base_ref_name
    await jevrev.github.gh.gh.clone(
        repo=repo, dest=dest, git_args=["--no-checkout"]
    )
    await git.fetch(dest, f"+refs/pull/{pr}/head:refs/pr/{pr}/head")
    head_sha, target_sha = await git.rev_parse(
        dest, [f"refs/pr/{pr}/head", f"refs/remotes/origin/{target}"]
    )
    merge_base = await git.merge_base(
        root=dest, first=head_sha, second=target_sha
    )
    await git.checkout_detached(dest, head_sha)
    return jevrev.db.PullRequestSource(
        repo=repo,
        pr=pr,
        head_sha=head_sha,
        target=target,
        target_sha=target_sha,
        merge_base=merge_base,
    )


async def _cmd_scan(args):
    db: pathlib.Path = args.db
    if db.exists():
        raise UserError(f"{db} already exists")
    if args.rules is None:
        raise UserError("Give --rules or set JEV_RULES_DIR.")
    metadata = dict(args.metadata)
    if len(metadata) != len(args.metadata):
        keys = [key for key, _ in args.metadata]
        raise UserError(
            "--metadata: keys given more than once: "
            + ", ".join(sorted({k for k in keys if keys.count(k) > 1}))
        )
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
        pull_request = await _clone_pull_request(
            repo=args.repo, pr=args.pr, dest=root
        )
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
            jevrev.state.Evaluation.HUNK,
            jevrev.state.Evaluation.FILE,
        ]
    found, summary = await _run_jev_scan(
        root=root,
        rules=args.rules,
        extra_rules=args.extra_rules,
        base=base,
        evaluate=evaluate,
    )
    review = jevrev.db.Review(
        dir=root,
        base=base,
        full=args.full,
        pull_request=pull_request,
        metadata=metadata,
        findings=[
            jevrev.db.Finding(
                **finding.model_dump(exclude={"file"}),
                file=str(pathlib.Path(finding.file).relative_to(root)),
                index=index,
            )
            for index, finding in enumerate(found, 1)
        ],
    )
    try:
        with jevrev.db.open_db(db, args.lock_timeout) as store:
            store.save(review)
    except BaseException:
        db.unlink(missing_ok=True)
        raise
    print(summary)


async def _run_jev_scan(root, rules, extra_rules, base=None, evaluate=None):
    """Run jev-scan on root against the rule sets under rules and each of
    extra_rules: its findings, and its summary line. Fails if any jev
    request fails."""
    settings = jevrev.settings.load_settings()
    if settings.api_key is None:
        raise UserError(
            "TYPESAFE_API_KEY is not set in the environment or TYPESAFE_ENV_FILE"
        )
    client = jevrev.jev.api.Client(
        api_key=settings.api_key.get_secret_value(),
        base_url=settings.base_url,
        timeout=settings.timeout,
    )
    try:
        async with asyncio.timeout(_JEV_SCAN_TIMEOUT):
            root, scanned = await jevrev.jev.scan.scan_states(
                rules,
                root=root,
                extra_rules=extra_rules,
                base=base,
                evaluate=evaluate,
            )
            found, failures = await jevrev.jev.scan.review(
                scanned=scanned,
                root=root,
                concurrency=_JEV_SCAN_CONCURRENCY,
                verbose=False,
                client=client,
                model=settings.default_model,
            )
    except TimeoutError:
        raise UserError(
            f"jev-scan ran longer than {_JEV_SCAN_TIMEOUT} seconds"
        )
    except (
        jevrev.jev.scan.UserError,
        jevrev.jev.ask.UserError,
        jevrev.jev.api.UserError,
    ) as e:
        raise UserError(f"jev-scan: {e}")
    if failures:
        raise UserError(
            f"jev-scan: {len(failures)} jev requests failed:\n"
            + "\n".join(failures)
        )
    summary = jevrev.jev.scan.summary_line(
        scanned=scanned, root=root, found=found, failures=failures
    )
    return found, summary


async def _cmd_pending(args):
    with _open_review(args.db, args.lock_timeout) as store:
        review = _load_review(store)
    sys.stdout.write(
        "".join(
            jevrev.jev.scan.json_dumps(
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


async def _cmd_confirm(args):
    await _judge(args, jevrev.db.Verdict.CONFIRMED)


async def _cmd_reject(args):
    await _judge(args, jevrev.db.Verdict.REJECTED)


async def _judge(args, verdict: jevrev.db.Verdict):
    index = args.index
    with _open_review(args.db, args.lock_timeout) as store:
        review = _load_review(store)
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


async def _cmd_show(args):
    review = _judged_review(args)
    match args.format:
        case Format.JSON:
            print(review.model_dump_json())
        case Format.MD:
            print(_review_text(review))


async def _cmd_post(args):
    review = _judged_review(args)
    source = review.pull_request
    if source is None:
        raise UserError("The review is not of a pull request.")
    if not _confirmed(review.findings):
        print("No confirmed findings; no GitHub review submitted.")
        return
    gh = jevrev.github.gh.gh
    pull_request = await gh.pull_request(source.repo, source.pr)
    user = await gh.current_user(source.repo.host)
    url = await gh.post_review(
        repo=source.repo,
        pr=source.pr,
        review=_pr_review(
            source=source,
            findings=review.findings,
            hunks=await _github_hunks(review.dir, source),
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
_TEMPLATES = minijinja.Environment(
    keep_trailing_newline=True,
    templates={
        Template.REVIEW: jevrev.templates.REVIEW,
        Template.PR_REVIEW: jevrev.templates.PR_REVIEW,
        Template.PR_COMMENT: jevrev.templates.PR_COMMENT,
    },
)


def _render(template: Template, model: pydantic.BaseModel) -> str:
    return _TEMPLATES.render_template(template, model=model)


class ReportRule(pydantic.BaseModel):
    """A rule's confirmed findings."""

    flags: str
    findings: list[jevrev.db.Finding]


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


def _confirmed(findings: list[jevrev.db.Finding]) -> list[jevrev.db.Finding]:
    return [f for f in findings if f.verdict == jevrev.db.Verdict.CONFIRMED]


def _by_rule(
    findings: list[jevrev.db.Finding],
) -> dict[str, list[jevrev.db.Finding]]:
    """findings grouped by rule, each group in order of file and line."""
    grouped: dict[str, list[jevrev.db.Finding]] = {}
    for f in sorted(findings, key=lambda f: (f.file, f.start)):
        grouped.setdefault(f.flags, []).append(f)
    return grouped


def _front_matter(review: jevrev.db.Review) -> str:
    front = {
        **review.model_dump(
            mode="json",
            exclude={"findings"}
            | (set() if review.metadata else {"metadata"}),
            exclude_none=True,
        ),
        "findings": len(review.findings),
        "confirmed": len(_confirmed(review.findings)),
        "rejected": sum(
            f.verdict == jevrev.db.Verdict.REJECTED for f in review.findings
        ),
    }
    return yaml.safe_dump(front, allow_unicode=True, sort_keys=True).rstrip(
        "\n"
    )


def _review_text(review: jevrev.db.Review) -> str:
    return _render(
        Template.REVIEW,
        ReviewText(
            front_matter=_front_matter(review),
            rules=[
                ReportRule(flags=flags, findings=found)
                for flags, found in _by_rule(
                    _confirmed(review.findings)
                ).items()
            ],
        ),
    )


async def _github_hunks(
    root: pathlib.Path, source: jevrev.db.PullRequestSource
) -> dict[str, list[range]]:
    """The head's lines of each hunk of the pull request's diff as GitHub
    shows it, by path: the lines a review comment can be left on."""
    diff = await git.diff(
        root=root,
        base=source.merge_base,
        head=source.head_sha,
        context=_GITHUB_DIFF_CONTEXT,
        inter_hunk_context=_GITHUB_INTER_HUNK_CONTEXT,
    )
    return {
        file.path: [
            range(hunk.target_start, hunk.target_start + hunk.target_length)
            for hunk in file
            if hunk.target_length
        ]
        for file in unidiff.PatchSet(diff)
    }


def _pr_comment(
    f: jevrev.db.Finding, hunks: dict[str, list[range]]
) -> jevrev.github.pull_request.ReviewComment:
    """A comment on f's lines within the first GitHub hunk they overlap, or
    on its file when f is a file finding or overlaps none."""
    body = _render(
        Template.PR_COMMENT, PrComment(reason=f.reason or "", flags=f.flags)
    )
    if f.evaluated != jevrev.state.Evaluation.FILE:
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


def _pr_review_body(
    found: list[jevrev.db.Finding], outside: list[jevrev.db.Finding]
) -> str:
    """The body of the review of found, whose findings in outside, which
    are in files the pull request did not change, are listed grouped by
    rule in as many as GitHub's limit has room for."""
    body = PrReviewBody(found=len(found))
    if not outside:
        return _render(Template.PR_REVIEW, body)

    def size(rules: list[ReportRule], left_out: int = 0) -> int:
        update = {"outside": rules, "left_out": left_out}
        return len(_render(Template.PR_REVIEW, body.model_copy(update=update)))

    base = size([])
    used = base
    # Room for the longest note of findings left out.
    room = _GITHUB_BODY_LIMIT - (size([], len(outside)) - base)
    rules: list[ReportRule] = []
    left_out = 0
    for flags, group in _by_rule(outside).items():
        empty = size([ReportRule(flags=flags, findings=[])])
        heading = empty - base
        kept: list[jevrev.db.Finding] = []
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
    return _render(
        Template.PR_REVIEW,
        body.model_copy(update={"outside": rules, "left_out": left_out}),
    )


def _pr_review(
    source: jevrev.db.PullRequestSource,
    findings: list[jevrev.db.Finding],
    hunks: dict[str, list[range]],
    event: jevrev.github.pull_request.ReviewEvent,
) -> jevrev.github.pull_request.NewReview:
    found = _confirmed(findings)
    # GitHub refuses a review comment on a file the pull request does not
    # change, which a full review's findings can be in.
    inside = [f for f in found if f.file in hunks]
    outside = [f for f in found if f.file not in hunks]
    return jevrev.github.pull_request.NewReview(
        commit_id=source.head_sha,
        event=event,
        body=_pr_review_body(found, outside),
        comments=[_pr_comment(f, hunks) for f in inside],
    )


def _positive_float(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not positive")
    return number


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not positive")
    return number


def _github_repo(value) -> jevrev.github.repo.GithubRepo:
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


def _add_judge_arguments(parser, reason_help):
    parser.add_argument(
        "index",
        type=_positive_int,
        metavar="INDEX",
        help="Index of the finding in the scan's findings, from 1",
    )
    parser.add_argument("reason", metavar="REASON", help=reason_help)


def _parser():
    parser = argparse.ArgumentParser(
        prog="jevrev",
        description="Run jev over a directory or a GitHub pull request, "
        "confirm or reject each of its findings, and print the review or "
        "post it on the pull request.",
    )
    db_group = parser.add_argument_group("db")
    db_group.add_argument(
        "--db",
        type=pathlib.Path,
        required=True,
        metavar="FILE",
        help="SQLite file holding one review: its dir, base, pull request, "
        "metadata, findings, and verdicts",
    )
    db_group.add_argument(
        "--lock-timeout",
        type=_positive_float,
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
    source_group = scan_parser.add_argument_group("source")
    dir_or_repo = source_group.add_mutually_exclusive_group(required=True)
    dir_or_repo.add_argument(
        "--dir",
        type=pathlib.Path,
        metavar="DIR",
        help="Directory to scan in place; findings are relative to it; "
        "requires --base or --full; conflicts with --repo",
    )
    dir_or_repo.add_argument(
        "--repo",
        type=_github_repo,
        metavar="HOST/ORG/NAME",
        help="GitHub repo of the pull request to clone and scan; requires "
        "--pr; conflicts with --dir",
    )
    source_group.add_argument(
        "--pr",
        type=_positive_int,
        metavar="N",
        help="Pull request number; requires --repo",
    )
    source_group.add_argument(
        "--cache-dir",
        type=pathlib.Path,
        metavar="DIR",
        help="Directory the pull request is cloned under, and left in "
        "[default: the temporary directory]",
    )
    change_group = scan_parser.add_argument_group("change")
    change_group.add_argument(
        "--base",
        metavar="REV",
        help="Revision of the git dir to compare from; requires --dir; "
        "conflicts with --full",
    )
    change_group.add_argument(
        "--full",
        action="store_true",
        help="Scan every file, not the change from the base or the pull "
        "request's merge base",
    )
    rules_group = scan_parser.add_argument_group("rules")
    rules_group.add_argument(
        "--rules",
        type=pathlib.Path,
        default=jevrev.jev.scan.env_path("JEV_RULES_DIR"),
        metavar="DIR",
        help="Directory of rule sets jev-scan reviews against "
        "[default: $JEV_RULES_DIR]; required",
    )
    rules_group.add_argument(
        "--extra-rules",
        type=jevrev.jev.scan.path_list,
        default=jevrev.jev.scan.path_list(
            os.environ.get("JEV_RULES_EXTRA", "")
        ),
        metavar="DIRS",
        help=f"More directories of rule sets, separated by {os.pathsep!r} "
        "[default: $JEV_RULES_EXTRA]",
    )
    scan_parser.add_argument(
        "--metadata",
        nargs=2,
        action="append",
        default=[],
        metavar=("KEY", "VALUE"),
        help="Metadata stored with the review and printed by show; repeat for more keys",
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
    _add_judge_arguments(
        confirm_parser, "One sentence on how the lines match the violation"
    )

    description = "Reject one finding without a verdict"
    reject_parser = subparsers.add_parser(
        "reject", help=description, description=description
    )
    _add_judge_arguments(
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


async def _main():
    args = _parser().parse_args()
    match args.command:
        case "scan":
            await _cmd_scan(args)
        case "pending":
            await _cmd_pending(args)
        case "confirm":
            await _cmd_confirm(args)
        case "reject":
            await _cmd_reject(args)
        case "show":
            await _cmd_show(args)
        case "post":
            await _cmd_post(args)


def cli():
    try:
        sys.exit(asyncio.run(_main()))
    except (
        UserError,
        jevrev.settings.UserError,
        jevrev.github.gh.GithubError,
        jevrev.db.OperationalError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()

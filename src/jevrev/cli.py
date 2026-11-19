"""Run jev over a directory or a GitHub pull request and print its findings, optionally into a
review's file; confirm or reject each of its findings, and print the review or post it on the pull
request once every finding has a verdict."""

import argparse
import asyncio
import collections.abc
import contextlib
import enum
import json
import math
import os
import pathlib
import sys
import traceback

import minijinja
import pydantic
import rich.console
import rich.text
import yaml
import yarl

import jevrev.files
import jevrev.git
import jevrev.github.api
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.jev.api
import jevrev.jev.ask
import jevrev.jev.scan
import jevrev.settings
import jevrev.state
import jevrev.store
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

# Exit statuses, as mypy's: no findings, findings, and any error.
_EXIT_CLEAN = 0
_EXIT_FINDINGS = 1
_EXIT_ERROR = 2


class UserError(Exception):
    pass


class Format(enum.StrEnum):
    JSON = "json"
    MD = "md"


def _review_path(args) -> pathlib.Path:
    if args.review is None:
        raise UserError(f"{args.command}: give --review")
    return args.review


@contextlib.contextmanager
def _open_review(path: pathlib.Path):
    if not path.is_file():
        raise UserError(f"{path}: no such review file")
    with jevrev.store.open_review(path) as store:
        yield store


def _save_new(path: pathlib.Path, review: jevrev.store.Review):
    """Save review into a new review file at path; a failure removes only the file this
    created."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
    except FileExistsError:
        raise UserError(f"{path} already exists") from None
    try:
        with jevrev.store.open_review(path) as store:
            store.save(review)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _judged_review(args) -> jevrev.store.Review:
    with _open_review(_review_path(args)) as store:
        review = store.load()
    if unjudged := [
        str(finding.index)
        for finding in review.findings
        if finding.verdict is None
    ]:
        raise UserError("Findings without a verdict: " + ", ".join(unjudged))
    return review


def _rules(args) -> list[pathlib.Path]:
    """--rules, or JEV_RULES when no --rules is given."""
    rules = args.rules
    if rules is None:
        rules = jevrev.settings.load_settings().rules
    if not rules:
        raise UserError("Give --rules or set JEV_RULES.")
    return rules


def _scan_metadata(args) -> dict[str, str]:
    file: pathlib.Path | None = args.review
    if file is not None and (file.exists() or file.is_symlink()):
        raise UserError(f"{file} already exists")
    if file is None and args.metadata:
        raise UserError("--metadata: give --review to store it in")
    _rules(args)
    _jev_client(args)
    # A later KEY wins; sorted by key, as the Rust version stores it.
    return dict(sorted(dict(args.metadata).items()))


def _jev_client(args) -> jevrev.jev.api.Client:
    """A client from the TypeSafe flags, each falling back to its setting."""
    settings = jevrev.settings.load_settings()
    api_key = args.api_key
    if api_key is None and settings.api_key is not None:
        api_key = settings.api_key.get_secret_value()
    if api_key is None:
        raise UserError("Give --api-key or set TYPESAFE_API_KEY.")
    return jevrev.jev.api.Client(
        api_key=api_key,
        base_url=args.base_url or settings.base_url,
        timeout=args.timeout or settings.timeout,
    )


def _local_dir(path: pathlib.Path) -> pathlib.Path:
    root = path.resolve()
    if not root.is_dir():
        raise UserError(f"{path} is not a directory")
    return root


async def _local_ignore(
    args,
    root: pathlib.Path,
    files: jevrev.files.Files,
    commit: jevrev.git.GitObjectId | None,
) -> jevrev.files.Ignore | None:
    if not args.ignore:
        return None
    prefix, above = await jevrev.git.git.ignore_files_above(root, commit)
    return await jevrev.files.Ignore.load(
        files,
        excludes=await jevrev.git.git.excludes(root),
        prefix=prefix,
        above=above,
    )


async def _scan_and_save(
    args,
    metadata: dict[str, str],
    source: jevrev.store.LocalDir
    | jevrev.store.LocalGitDir
    | jevrev.store.GithubRepo,
    name: str,
    files: jevrev.files.Files,
    ignore: jevrev.files.Ignore | None,
    diff: jevrev.git.Diff | None = None,
    untracked: collections.abc.Sequence[str] = (),
):
    found, summary = await _run_jev_scan(
        files=files,
        ignore=ignore,
        include=args.include,
        exclude=args.exclude,
        diff=diff,
        untracked=untracked,
        rules=_rules(args),
        name=name,
        client=_jev_client(args),
        model=args.model or jevrev.settings.load_settings().default_model,
    )
    review = jevrev.store.Review(
        source=source,
        metadata=metadata,
        include=args.include,
        exclude=args.exclude,
        findings=[
            jevrev.store.Finding(**finding.model_dump(), index=index)
            for index, finding in enumerate(found, 1)
        ],
    )
    if args.review is not None:
        _save_new(args.review, review)
    _print_findings(review.findings, summary)
    if review.findings and not args.exit_zero:
        return _EXIT_FINDINGS
    return _EXIT_CLEAN


def _probability_style(probability: float) -> str:
    if probability >= 0.9:
        return "bold red"
    if probability >= 0.7:
        return "yellow"
    return "default"


def _print_findings(findings: list[jevrev.store.Finding], summary: str):
    """Print findings as a linter does, in order of file and line, then summary; in color only
    when stdout is a terminal."""
    console = rich.console.Console(soft_wrap=True, highlight=False)
    for f in sorted(findings, key=lambda f: (f.file, f.start, f.index)):
        lines = f"{f.start}" if f.start == f.end else f"{f.start}-{f.end}"
        console.print(
            rich.text.Text.assemble(
                (f"{f.file}:{lines}", "bold"),
                " ",
                (f"[{f.index}]", "dim"),
                " ",
                (f.rule, "yellow"),
                " ",
                (f"{f.probability:.2f}", _probability_style(f.probability)),
                " ",
                (f.state, "cyan"),
            )
        )
        console.print(rich.text.Text(f"    {f.flags}"))
    if findings:
        console.print()
    console.print(rich.text.Text(summary))


async def _cmd_scan(args):
    """Review PATH's files on disk, whether or not PATH is in a git repository. A file is
    reviewed by its name, even when an ignore file matches it."""
    metadata = _scan_metadata(args)
    path: pathlib.Path = args.path
    if path.is_dir():
        root = path.resolve()
        files: jevrev.files.Files = jevrev.files.OnDisk(root)
        ignore = await _local_ignore(args, root=root, files=files, commit=None)
    elif path.is_file():
        # Its directory resolved, its own name kept, as a symlink's is its own.
        root = path.absolute().parent.resolve() / path.name
        files = jevrev.files.OneFile(root)
        ignore = None
    else:
        raise UserError(f"{path}: no such file or directory")
    return await _scan_and_save(
        args,
        metadata=metadata,
        source=jevrev.store.LocalDir(path=root),
        name=str(root),
        files=files,
        ignore=ignore,
    )


async def _cmd_scan_git(args):
    """Review DIR at --rev or --diff, resolved to object ids before jev runs. Never
    writes to DIR, its repository or its HEAD."""
    metadata = _scan_metadata(args)
    root = _local_dir(args.dir)
    if not await jevrev.git.git.in_work_tree(root):
        raise UserError(f"{root} is not in a git repository")
    diff = None
    untracked: list[str] = []
    if args.rev is not None:
        selection = await jevrev.git.git.resolve_rev(root, args.rev)
        files = jevrev.git.CommitFiles(root, selection.commit)
        commit = selection.commit
    else:
        selection = await jevrev.git.git.resolve_diff(root, args.diff)
        match selection:
            case jevrev.git.GitDiff(base=base, head=head):
                files = jevrev.git.CommitFiles(root, head)
                commit = head
                diff = await jevrev.git.git.diff_commits(
                    root=root,
                    base=base,
                    head=head,
                    context=jevrev.jev.scan.DIFF_CONTEXT,
                    inter_hunk_context=0,
                )
            case jevrev.git.GitDiffToDisk(base=base):
                files = jevrev.files.OnDisk(root)
                commit = None
                diff = await jevrev.git.git.diff_work_tree(
                    root=root,
                    revision=base,
                    context=jevrev.jev.scan.DIFF_CONTEXT,
                    pathspecs=["."],
                )
                untracked = await jevrev.git.git.untracked_files(
                    root, ["."], ignored=not args.ignore
                )
    return await _scan_and_save(
        args,
        metadata=metadata,
        source=jevrev.store.LocalGitDir(path=root, git=selection),
        name=str(root),
        files=files,
        ignore=await _local_ignore(
            args, root=root, files=files, commit=commit
        ),
        diff=diff,
        untracked=untracked,
    )


async def _github_client(host: str) -> jevrev.github.api.Client:
    return jevrev.github.api.Client(
        host=host, token=await jevrev.github.api.token(host)
    )


async def _github_selection(
    args,
    client: jevrev.github.api.Client,
    repo: jevrev.github.repo.GithubRepo,
) -> (
    jevrev.git.GitRev
    | jevrev.git.GitDiff
    | jevrev.github.pull_request.GithubPullRequest
):
    if args.pr is not None:
        pull_request = await client.pull_request(repo, args.pr)
        return jevrev.github.pull_request.GithubPullRequest(
            base=await client.merge_base(
                repo, pull_request.target, pull_request.head
            ),
            head=pull_request.head,
            number=args.pr,
        )
    if args.diff is not None:
        spec: jevrev.git.GitDiffSpec = args.diff
        base = await client.commit(repo, spec.base)
        head = await client.commit(repo, spec.head)
        if spec.merge_base:
            base = await client.merge_base(repo, base, head)
        return jevrev.git.GitDiff(base=base, head=head)
    if args.rev is not None:
        return jevrev.git.GitRev(commit=await client.commit(repo, args.rev))
    return jevrev.git.GitRev(commit=await client.default_branch_head(repo))


async def _cmd_scan_github(args):
    """Review a GitHub repo through the GitHub API on the host GH_HOST names."""
    metadata = _scan_metadata(args)
    if args.diff is not None and args.diff.head is None:
        raise UserError(
            "--diff: give A..B or A...B; a GitHub repo has no files on disk"
        )
    repo: jevrev.github.repo.GithubRepo = args.github
    async with await _github_client(repo.host) as client:
        selection = await _github_selection(args, client=client, repo=repo)
        diff = None
        if isinstance(selection, jevrev.git.GitRev):
            files = await client.tarball(repo, selection.commit)
        else:
            files = await client.tree(repo, selection.head)
            diff = await client.diff(
                repo,
                base=selection.base,
                head=selection.head,
                context=jevrev.jev.scan.DIFF_CONTEXT,
                inter_hunk_context=0,
            )
        ignore = None
        if args.ignore:
            ignore = await jevrev.files.Ignore.load(
                files, excludes=await jevrev.git.git.global_excludes()
            )
        return await _scan_and_save(
            args,
            metadata=metadata,
            source=jevrev.store.GithubRepo(**repo.model_dump(), git=selection),
            name=f"{repo.host}/{repo.org}/{repo.name}",
            files=files,
            ignore=ignore,
            diff=diff,
        )


async def _run_jev_scan(
    files: jevrev.files.Files,
    ignore: jevrev.files.Ignore | None,
    include: list[str],
    exclude: list[str],
    diff: jevrev.git.Diff | None,
    untracked: collections.abc.Sequence[str],
    rules: list[pathlib.Path],
    name: str,
    client: jevrev.jev.api.Client,
    model: str,
):
    """Run jev-scan on files against the rule sets under each directory of rules: its
    findings, and its summary line. Fails if any jev request fails."""
    try:
        async with asyncio.timeout(_JEV_SCAN_TIMEOUT):
            scanned = await jevrev.jev.scan.scan_states(
                rules,
                files=files,
                ignore=ignore,
                diff=diff,
                untracked=untracked,
                include=include,
                exclude=exclude,
            )
            found, failures = await jevrev.jev.scan.review(
                scanned=scanned,
                concurrency=_JEV_SCAN_CONCURRENCY,
                verbose=False,
                client=client,
                model=model,
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
        scanned=scanned, source=name, found=found, failures=failures
    )
    return found, summary


async def _cmd_pending(args):
    with _open_review(_review_path(args)) as store:
        review = store.load()
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
    await _judge(args, jevrev.store.Verdict.CONFIRMED)


async def _cmd_reject(args):
    await _judge(args, jevrev.store.Verdict.REJECTED)


async def _judge(args, verdict: jevrev.store.Verdict):
    index = args.index
    with _open_review(_review_path(args)) as store:
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


async def _cmd_show(args):
    review = _judged_review(args)
    match args.format:
        case Format.JSON:
            print(review.model_dump_json())
        case Format.MD:
            print(_review_text(review))


async def _cmd_submit_github(args):
    review = _judged_review(args)
    source = review.source
    if not isinstance(source, jevrev.store.GithubRepo) or not isinstance(
        source.git, jevrev.github.pull_request.GithubPullRequest
    ):
        raise UserError("The review is not of a GitHub pull request.")
    if not _confirmed(review.findings):
        print("No confirmed findings; no GitHub review submitted.")
        return
    pr = source.git
    async with await _github_client(source.host) as client:
        pull_request = await client.pull_request(source, pr.number)
        user = await client.current_user()
        diff = await client.diff(
            source,
            base=pr.base,
            head=pr.head,
            context=_GITHUB_DIFF_CONTEXT,
            inter_hunk_context=_GITHUB_INTER_HUNK_CONTEXT,
        )
        url = await client.post_review(
            repo=source,
            pr=pr.number,
            review=_pr_review(
                head=pr.head,
                findings=review.findings,
                hunks=_github_hunks(diff),
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
    findings: list[jevrev.store.Finding]


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


def _confirmed(
    findings: list[jevrev.store.Finding],
) -> list[jevrev.store.Finding]:
    return [f for f in findings if f.verdict == jevrev.store.Verdict.CONFIRMED]


def _by_rule(
    findings: list[jevrev.store.Finding],
) -> dict[str, list[jevrev.store.Finding]]:
    """findings grouped by rule, each group in order of file and line."""
    grouped: dict[str, list[jevrev.store.Finding]] = {}
    for f in sorted(findings, key=lambda f: (f.file, f.start)):
        grouped.setdefault(f.flags, []).append(f)
    return grouped


def _front_matter(review: jevrev.store.Review) -> str:
    front = {
        **review.model_dump(
            mode="json",
            exclude={"findings"}
            | (set() if review.metadata else {"metadata"})
            | (set() if review.include else {"include"})
            | (set() if review.exclude else {"exclude"}),
            exclude_none=True,
        ),
        "findings": len(review.findings),
        "confirmed": len(_confirmed(review.findings)),
        "rejected": sum(
            f.verdict == jevrev.store.Verdict.REJECTED for f in review.findings
        ),
    }
    return yaml.safe_dump(front, allow_unicode=True, sort_keys=True).rstrip(
        "\n"
    )


def _review_text(review: jevrev.store.Review) -> str:
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


def _github_hunks(diff: jevrev.git.Diff) -> dict[str, list[range]]:
    """The head's lines of each hunk of the pull request's diff as GitHub
    shows it, by path: the lines a review comment can be left on."""
    return {
        file.path: [
            range(hunk.target_start, hunk.target_start + hunk.target_length)
            for hunk in file.hunks
            if hunk.target_length
        ]
        for file in diff.root
    }


def _pr_comment(
    f: jevrev.store.Finding, hunks: dict[str, list[range]]
) -> jevrev.github.pull_request.ReviewComment:
    body = _render(
        Template.PR_COMMENT, PrComment(reason=f.reason or "", flags=f.flags)
    )
    if f.state in jevrev.state.LINE_KINDS:
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
    found: list[jevrev.store.Finding], outside: list[jevrev.store.Finding]
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
        kept: list[jevrev.store.Finding] = []
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
    head: jevrev.git.GitObjectId,
    findings: list[jevrev.store.Finding],
    hunks: dict[str, list[range]],
    event: jevrev.github.pull_request.ReviewEvent,
) -> jevrev.github.pull_request.NewReview:
    found = _confirmed(findings)
    # GitHub refuses a review comment on a file the pull request does not
    # change, which a full review's findings can be in.
    inside = [f for f in found if f.file in hunks]
    outside = [f for f in found if f.file not in hunks]
    return jevrev.github.pull_request.NewReview(
        commit_id=head,
        event=event,
        body=_pr_review_body(found, outside),
        comments=[_pr_comment(f, hunks) for f in inside],
    )


def _path_list(value: str) -> list[pathlib.Path]:
    """The ':'-separated directories of value, empty entries left out."""
    return [pathlib.Path(entry) for entry in value.split(":") if entry]


def _base_url(value: str) -> yarl.URL:
    try:
        url = yarl.URL(value)
    except ValueError:
        url = None
    if url is None or not url.absolute:
        raise argparse.ArgumentTypeError(
            f"{json.dumps(value)} is not an absolute URL"
        )
    return yarl.URL(value.rstrip("/"))


def _timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError:
        timeout = math.nan
    if not timeout > 0:
        raise argparse.ArgumentTypeError(
            f"{json.dumps(value)} is not a positive number"
        )
    return timeout


def _metadata(value: str) -> tuple[str, str]:
    key, sep, rest = value.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(
            f"{json.dumps(value)} is not KEY=VALUE with a KEY"
        )
    return key, rest


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f"{value} is not positive")
    return number


def _git_revision(value) -> jevrev.git.GitRevision:
    try:
        return pydantic.TypeAdapter(jevrev.git.GitRevision).validate_python(
            value
        )
    except pydantic.ValidationError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not a revision") from e


def _github_repo(value) -> jevrev.github.repo.GithubRepo:
    """ORG/NAME, on the host GH_HOST names."""
    org, slash, name = value.partition("/")
    if not slash:
        raise argparse.ArgumentTypeError(f"{value}: expected ORG/NAME")
    try:
        return jevrev.github.repo.GithubRepo(
            host=jevrev.github.api.host(), org=org, name=name
        )
    except pydantic.ValidationError as e:
        problems = "; ".join(
            problem["msg"] for problem in e.errors(include_url=False)
        )
        raise argparse.ArgumentTypeError(f"{value}: {problems}") from e


def _git_diff_spec(value) -> jevrev.git.GitDiffSpec:
    try:
        return jevrev.git.GitDiffSpec.parse(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not a diff") from e


def _add_scan_arguments(parser):
    rules_group = parser.add_argument_group("rules")
    rules_group.add_argument(
        "--rules",
        type=_path_list,
        action="extend",
        metavar="DIR",
        help="Directory of rule sets jev-scan reviews against; repeat for "
        "more directories, or give a ':'-separated list in JEV_RULES [env: "
        "JEV_RULES]",
    )
    api_group = parser.add_argument_group("typesafe")
    api_group.add_argument(
        "--api-key",
        metavar="API_KEY",
        help="TypeSafe API key [env: TYPESAFE_API_KEY]",
    )
    api_group.add_argument(
        "--base-url",
        type=_base_url,
        metavar="BASE_URL",
        help="TypeSafe API base URL, an absolute URL [env: TYPESAFE_BASE_URL] "
        f"[default: {jevrev.jev.api.DEFAULT_BASE_URL}]",
    )
    api_group.add_argument(
        "--model",
        metavar="MODEL",
        help="Jev model to ask [env: TYPESAFE_DEFAULT_MODEL] [default: "
        f"{jevrev.jev.api.DEFAULT_MODEL}]",
    )
    api_group.add_argument(
        "--timeout",
        type=_timeout,
        metavar="TIMEOUT",
        help="Seconds each connection and read to the TypeSafe API may take; "
        "inf for no limit [env: TYPESAFE_TIMEOUT] [default: "
        f"{jevrev.settings.Settings.model_fields['timeout'].default:g}]",
    )
    ignore_group = parser.add_argument_group("ignore")
    ignore_group.add_argument(
        "--ignore",
        dest="ignore",
        action="store_true",
        default=True,
        help="Leave out every file .gitignore, .ignore, .git/info/exclude or "
        "core.excludesFile matches [default]; the last of --ignore and "
        "--no-ignore wins",
    )
    ignore_group.add_argument(
        "--no-ignore",
        dest="ignore",
        action="store_false",
        help="Scan files an ignore file matches too",
    )
    parser.add_argument(
        "-i",
        "--include",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Scan only files PATTERN matches, in gitignore syntax relative "
        "to the source, of those the ignore files leave; repeat for more "
        "patterns, any of which may match",
    )
    parser.add_argument(
        "-e",
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Leave out files PATTERN matches, in gitignore syntax relative "
        "to the source; repeat for more patterns; wins over --include",
    )
    parser.add_argument(
        "--metadata",
        type=_metadata,
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Metadata stored with the review and printed by show; repeat "
        "for more keys, a later KEY winning; KEY is not empty; requires "
        "--review",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Exit 0 even when the scan has findings",
    )


_REVIEW_FLOW = """\
review flow:
  1. jevrev --review FILE scan|scan-git|scan-github ...
     Scan, and save the findings to FILE, a new review file.
  2. jevrev --review FILE pending
     Print the findings without a verdict.
  3. jevrev --review FILE confirm|reject INDEX REASON
     Record one finding's verdict; repeat until pending prints nothing.
  4. jevrev --review FILE show [--format json|md]
     Print the review; or, for a scan-github --pr review:
     jevrev --review FILE submit-github
     Post the confirmed findings on the pull request.

Run `jevrev COMMAND --help` for each command's options."""

_SCAN_NOTES = (
    "TYPESAFE_ENV_FILE may name a dotenv file of the TYPESAFE_ variables; "
    "the environment's own values win. A scan fails once it has run "
    f"{_JEV_SCAN_TIMEOUT // 60} minutes."
)

_GITHUB_SCAN_NOTES = (
    f"{_SCAN_NOTES} The GitHub token is GH_TOKEN or GITHUB_TOKEN for "
    "github.com, GH_ENTERPRISE_TOKEN or GITHUB_ENTERPRISE_TOKEN for another "
    "host, or else `gh auth token`'s."
)

_PENDING_FIELDS = """\
fields of each line:
  index        the finding's INDEX for confirm and reject, from 1
  file         the finding's file, relative to the scanned source
  start, end   the finding's first and last lines
  rule         the rule's id: its rule set's file name and its position in
               it, from 1, such as text.yaml#1
  probability  how likely jev judges the rule broken, 0 to 1
  flags        what the rule flags, as its rule set states it
  violation    the rule's examples of what breaks it
  accepted     the rule's examples of what keeps it
  state        what jev was given of the file: "file", the whole file; "hunk",
               one hunk of a change; "hunks", all of a file's changed hunks;
               or "language", one range of the file that a language yields"""

_JUDGE_NOTES = """\
A finding's verdict cannot be changed. A command holds the review file's lock
while it runs, and any other command on the file fails at once, so run confirm
and reject one at a time."""

_SUBMIT_NOTES = """\
Findings in files the pull request changes become comments on their lines;
the others are listed in the review's body. The review requests changes,
except on a pull request by the token's own user, where it is a comment. With
no confirmed findings, it posts nothing. It fails when the review is not of a
pull request, such as one of a --rev or --diff scan."""


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
        description="Run jev over a directory or a GitHub pull request and "
        "print its findings, confirm or reject each of them, and print the "
        "review or post it on the pull request. Exits 0 with no findings, 1 "
        "with findings, and 2 on any error.",
        epilog=_REVIEW_FLOW,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    review_group = parser.add_argument_group("review")
    review_group.add_argument(
        "-r",
        "--review",
        type=pathlib.Path,
        metavar="FILE",
        help="JSON file holding one review: its source, the version of it "
        "scanned, metadata, findings, and verdicts; a scan given it creates "
        "FILE, which must not exist (checked before any request), and leaves "
        "no FILE it created when the scan or its save fails; every other "
        "command requires it",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND"
    )

    description = (
        "Run jev over a file, or a directory's files, on disk, whether or not "
        "it is in a git repository; print its findings and summary, and save "
        "them to a new review file given --review"
    )
    scan_parser = subparsers.add_parser(
        "scan",
        help=description,
        description=description,
        epilog=_SCAN_NOTES,
    )
    scan_parser.add_argument(
        "path",
        type=pathlib.Path,
        metavar="PATH",
        help="File or directory to scan, symlinks followed; findings are "
        "relative to the directory, or the file's own; a file is scanned "
        "even when an ignore file matches it",
    )
    _add_scan_arguments(scan_parser)

    description = (
        "Run jev over a git directory at a revision, or over a diff; print "
        "its findings and summary, and save them to a new review file given --review"
    )
    scan_git_parser = subparsers.add_parser(
        "scan-git",
        help=description,
        description=description,
        epilog=_SCAN_NOTES,
    )
    scan_git_parser.add_argument(
        "dir",
        type=pathlib.Path,
        metavar="DIR",
        help="Directory in a git repository to scan; findings are relative "
        "to it",
    )
    git_group = scan_git_parser.add_argument_group("git")
    selection = git_group.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--rev",
        type=_git_revision,
        metavar="REV",
        help="Scan the files at REV",
    )
    selection.add_argument(
        "--diff",
        type=_git_diff_spec,
        metavar="DIFF",
        help="Scan a diff, in git diff's syntax: REV, the changes from REV "
        "to the files on disk; A..B, from A to B; A...B, from merge-base(A, "
        "B) to B; an empty side of A..B or A...B is HEAD",
    )
    _add_scan_arguments(scan_git_parser)

    description = (
        "Run jev over a GitHub repo, through the GitHub API on the host "
        "GH_HOST names (github.com when unset); print its findings and "
        "summary, and save them to a new review file given --review"
    )
    scan_github_parser = subparsers.add_parser(
        "scan-github",
        help=description,
        description=description,
        epilog=_GITHUB_SCAN_NOTES,
    )
    scan_github_parser.add_argument(
        "github",
        type=_github_repo,
        metavar="ORG/NAME",
        help="GitHub repo to scan; findings are relative to its top",
    )
    github_group = scan_github_parser.add_argument_group("github")
    github_selection = github_group.add_mutually_exclusive_group()
    github_selection.add_argument(
        "--rev",
        type=_git_revision,
        metavar="REV",
        help="Scan the files at REV [default: the head of the default branch]",
    )
    github_selection.add_argument(
        "--diff",
        type=_git_diff_spec,
        metavar="A..B",
        help="Scan a diff, in git diff's syntax: A..B, the changes from A to "
        "B; A...B, from merge-base(A, B) to B; an empty side is HEAD",
    )
    github_selection.add_argument(
        "--pr",
        type=_positive_int,
        metavar="N",
        help="Scan pull request N's changes",
    )
    _add_scan_arguments(scan_github_parser)

    description = (
        "Print each finding without a verdict as a line of JSON, its file "
        "relative to the source"
    )
    subparsers.add_parser(
        "pending",
        help=description,
        description=description,
        epilog=_PENDING_FIELDS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    description = "Confirm one finding without a verdict"
    confirm_parser = subparsers.add_parser(
        "confirm",
        help=description,
        description=description,
        epilog=_JUDGE_NOTES,
    )
    _add_judge_arguments(
        confirm_parser, "One sentence on how the lines match the violation"
    )

    description = "Reject one finding without a verdict"
    reject_parser = subparsers.add_parser(
        "reject",
        help=description,
        description=description,
        epilog=_JUDGE_NOTES,
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
        "Submit the confirmed findings as a review on the GitHub pull "
        "request and print its URL, once every finding has a verdict; fails "
        "while any finding has none"
    )
    subparsers.add_parser(
        "submit-github",
        help=description,
        description=description,
        epilog=_SUBMIT_NOTES,
    )
    return parser


async def _main():
    args = _parser().parse_args()
    match args.command:
        case "scan":
            return await _cmd_scan(args)
        case "scan-git":
            return await _cmd_scan_git(args)
        case "scan-github":
            return await _cmd_scan_github(args)
        case "pending":
            await _cmd_pending(args)
        case "confirm":
            await _cmd_confirm(args)
        case "reject":
            await _cmd_reject(args)
        case "show":
            await _cmd_show(args)
        case "submit-github":
            await _cmd_submit_github(args)
    return _EXIT_CLEAN


def cli():
    try:
        status = asyncio.run(_main())
    except (
        UserError,
        jevrev.settings.UserError,
        jevrev.github.api.GithubError,
        jevrev.git.GitError,
        jevrev.store.InvalidReview,
        OSError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(_EXIT_ERROR)
    except Exception:
        traceback.print_exc()
        sys.exit(_EXIT_ERROR)
    sys.exit(status)


if __name__ == "__main__":
    cli()

"""Scan code and text with jev rule sets: each definition, run of top-level code or whole file of
the files in a known language under a root, or the hunks of any text file changed since a
revision. Each file is reviewed against the rule sets under the rules directory whose `when`
matches it."""

import asyncio
import dataclasses
import enum
import functools
import json
import os
import pathlib
import re
import sys

import pathspec
import pydantic
import unidiff
import yaml

import jevrev.jev.api
import jevrev.jev.ask
import jevrev.languages
import jevrev.languages.base
import jevrev.process
import jevrev.rules
import jevrev.state


@functools.cache
def _read_set(path):
    try:
        return jevrev.rules.RuleSet.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, pydantic.ValidationError) as e:
        raise UserError(f"rule set {path}: {e}") from e


@dataclasses.dataclass(frozen=True)
class Condition:
    """One `when` entry; an empty field matches every file."""

    languages: frozenset[jevrev.rules.Language]
    patterns: pathspec.GitIgnoreSpec | None
    # Each matches at least one path under the root.
    exists: tuple[pathspec.Pattern, ...]

    def holds_under(self, root):
        return all(
            next(
                pathspec.GitIgnoreSpec([pattern]).match_tree_files(root), None
            )
            is not None
            for pattern in self.exists
        )

    def matches(self, detected, name):
        return (not self.languages or detected in self.languages) and (
            self.patterns is None or self.patterns.match_file(name)
        )


@dataclasses.dataclass(frozen=True)
class RuleSet:
    path: pathlib.Path
    # The set reviews a file when any of these matches it.
    conditions: tuple[Condition, ...]

    def under(self, root):
        """The set as it applies under `root`: only the conditions whose paths exist there."""
        return dataclasses.replace(
            self,
            conditions=tuple(
                c for c in self.conditions if c.holds_under(root)
            ),
        )


@functools.cache
def _rule_sets(rules):
    """Every rule set under `rules` with a `when`, sorted by path."""
    if not rules.is_dir():
        raise UserError(f"rule sets directory {rules} is not a directory")
    found = []
    for path in sorted(rules.rglob("*.yaml")):
        parsed = _read_set(path)
        if parsed.when is None:
            continue
        found.append(
            RuleSet(
                path=path,
                conditions=tuple(
                    Condition(
                        languages=frozenset(when.language),
                        patterns=pathspec.GitIgnoreSpec(when.matches)
                        if when.matches
                        else None,
                        exists=when.exists,
                    )
                    for when in parsed.when
                ),
            )
        )
    return tuple(found)


def env_path(name):
    """The path in the environment variable name, or None when it is unset or empty."""
    value = os.environ.get(name)
    return pathlib.Path(value) if value else None


def path_list(value):
    """The paths of a list separated by os.pathsep, as in PATH, without its empty entries."""
    return [pathlib.Path(entry) for entry in value.split(os.pathsep) if entry]


def _sets_under(sets, root):
    """The rule sets that can apply under `root`, each with only its conditions that do."""
    return tuple(s for s in (s.under(root) for s in sets) if s.conditions)


@dataclasses.dataclass(frozen=True)
class Review:
    """How a file is reviewed."""

    # The rule sets every state of the file is reviewed against.
    sets: tuple[pathlib.Path, ...]
    # How the file is split into states and where its test code starts.
    language: type[jevrev.languages.base.Language]


def _review_of(name, source, sets):
    """How a file is reviewed against `sets`; `name` is its path from the root."""
    language = jevrev.languages.detect(name, source)
    return Review(
        sets=tuple(
            rule_set.path
            for rule_set in sets
            if any(c.matches(language.name, name) for c in rule_set.conditions)
        ),
        language=language,
    )


def _in_whole_scan(name, source):
    """Whether a whole-directory scan reviews the file: it has a language. With --base, every
    changed text file is reviewed."""
    return jevrev.languages.detect(name, source).name is not None


class UserError(Exception):
    pass


def _inside(root, path):
    """Whether path, its symlinks followed, is under root, which is resolved."""
    return path.resolve().is_relative_to(root)


class Finding(pydantic.BaseModel):
    file: str
    start: int
    end: int
    item: str
    # The rule set's file name and the rule's position in it, such as text.yaml#1.
    rule: str
    probability: float
    flags: str
    violation: str
    accepted: str
    evaluated: jevrev.state.Evaluation | None = None


class AskFailed(Exception):
    def __init__(self, state, stderr):
        super().__init__(state, stderr)
        self.state = state
        self.stderr = stderr

    def __str__(self):
        return f"{self.state.file}:{self.state.start} {self.state.name}: {self.stderr.strip()}"


def json_dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class git:
    @staticmethod
    async def _run(root: pathlib.Path, argv: list[str]) -> str:
        """The stdout of git, in root, with argv."""
        try:
            stdout = await jevrev.process.run(["git", *argv], cwd=root)
        except jevrev.process.ProcessError as e:
            raise UserError(str(e)) from e
        return stdout.decode()

    @staticmethod
    async def current_repository():
        try:
            stdout = await jevrev.process.run(
                ["git", "rev-parse", "--show-toplevel"], cwd=pathlib.Path.cwd()
            )
        except jevrev.process.ProcessError as e:
            raise UserError(
                "the current directory is outside a git repository; give --root"
            ) from e
        return pathlib.Path(stdout.decode().strip())

    @staticmethod
    async def in_repository(root):
        try:
            stdout = await jevrev.process.run(
                ["git", "rev-parse", "--is-inside-work-tree"], cwd=root
            )
        except jevrev.process.ProcessError:
            return False
        return stdout.decode().strip() == "true"

    @staticmethod
    async def merge_base(root, base):
        """The merge-base of `base` and HEAD."""
        return (await git._run(root, ["merge-base", base, "HEAD"])).strip()

    @staticmethod
    async def diff(root, revision, context, pathspecs):
        """The diff from `revision` to the working tree of the files matching `pathspecs`, with
        `context` as git diff options for its context lines."""
        return await git._run(
            root,
            [
                "diff",
                *context,
                "--no-color",
                "--no-ext-diff",
                "--relative",
                revision,
                "--",
                *pathspecs,
            ],
        )

    @staticmethod
    async def untracked_files(root, pathspecs):
        """The untracked files matching `pathspecs`, leaving out those git ignores."""
        stdout = await git._run(
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
        return [name for name in stdout.split("\0") if name]

    @staticmethod
    async def tracked_files(root, pathspecs):
        """The tracked and untracked files matching `pathspecs`, leaving out those git ignores."""
        stdout = await git._run(
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
        return [name for name in stdout.split("\0") if name]


def _file_contents(name, source, review):
    return [
        content.model_copy(update={"sets": review.sets})
        for content in review.language.contents(source, name)
    ]


def _merge_constants(contents):
    """Consecutive const, static and assignment contents as one, so they cost one review together."""
    merged = []
    for content in contents:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.file == content.file
            and previous.context == content.context
            and previous.kind in jevrev.state.MERGED
            and content.kind in jevrev.state.MERGED
            and content.start - previous.end <= 2
        ):
            gap = "\n" * (content.start - previous.end)
            merged[-1] = previous.model_copy(
                update={
                    "end": content.end,
                    "name": f"{previous.name}, {content.name}",
                    "text": previous.text + gap + content.text,
                }
            )
        else:
            merged.append(content)
    return merged


def _test_cutoff(source, marker):
    """The line number of the first line that is `marker`, after the last line when there is none."""
    lines = source.decode(errors="replace").splitlines()
    return next(
        (
            n
            for n, line in enumerate(lines, 1)
            if marker is not None and line == marker
        ),
        len(lines) + 1,
    )


async def _changed_hunks(root, pathspecs, base, context, sets):
    """Each hunk changed since `base`'s merge-base with HEAD, with `context` as git diff options
    for its context lines, as a diff of its file; and the untracked files."""
    if base.startswith("-"):
        raise UserError(f"base {base!r} is not a revision")
    merge_base = await git.merge_base(root, base)
    diff = await git.diff(
        root=root, revision=merge_base, context=context, pathspecs=pathspecs
    )
    changed = []
    for patched in unidiff.PatchSet(diff):
        if patched.is_removed_file or patched.is_binary_file:
            continue
        name = patched.path
        # A submodule's diff names its directory.
        if not (root / name).is_file() or not _inside(root, root / name):
            continue
        source = (root / name).read_bytes()
        review = _review_of(name=name, source=source, sets=sets)
        cutoff = _test_cutoff(source, review.language.test_marker)
        for hunk in patched:
            lines = [
                line
                for line in hunk
                if (line.is_added or line.is_context)
                and line.target_line_no < cutoff
            ]
            if not any(line.is_added for line in lines):
                continue
            header = (
                f"@@ -{hunk.source_start},{hunk.source_length} "
                f"+{hunk.target_start},{hunk.target_length} @@ {hunk.section_header}"
            )
            changed.append(
                jevrev.state.FileDiff(
                    file=name,
                    start=hunk.target_start,
                    end=lines[-1].target_line_no,
                    name=hunk.section_header or jevrev.state.Evaluation.HUNK,
                    sets=review.sets,
                    hunks=[
                        jevrev.state.DiffHunk(
                            header=header.rstrip(),
                            lines=[
                                f"{line.line_type}{line.value.removesuffix('\n')}"
                                for line in lines
                            ],
                        )
                    ],
                )
            )
    return changed, await git.untracked_files(root, pathspecs)


_DEFAULT_CONTEXT = 20


def _added_everywhere(diff):
    """`diff` with every context line of its hunks marked as added, so it is judged too."""
    return diff.model_copy(
        update={
            "hunks": [
                hunk.model_copy(
                    update={"lines": [f"+{line[1:]}" for line in hunk.lines]}
                )
                for hunk in diff.hunks
            ]
        }
    )


def _file_diffs(hunks):
    """One diff per file holding all of the file's hunks."""
    by_file = {}
    for hunk in hunks:
        by_file.setdefault(hunk.file, []).append(hunk)
    return [
        jevrev.state.FileDiff(
            file=name,
            start=file_hunks[0].start,
            end=file_hunks[-1].end,
            name=jevrev.state.Evaluation.FILE,
            sets=file_hunks[0].sets,
            hunks=[hunk for diff in file_hunks for hunk in diff.hunks],
        )
        for name, file_hunks in by_file.items()
    ]


def _change_states(hunks, untracked, evaluations, all_added, sets):
    """The states that evaluate the change's `hunks` as each of `evaluations` says. An untracked
    file is all added, so its diff is the whole file, and its hunks are its top-level contents."""
    if all_added:
        hunks = [_added_everywhere(hunk) for hunk in hunks]
    states = []
    if jevrev.state.Evaluation.FILE in evaluations:
        new_files = []
        for name, source in untracked.items():
            review = _review_of(name=name, source=source, sets=sets)
            lines = source.decode(errors="replace").splitlines()
            lines = lines[
                : _test_cutoff(source, review.language.test_marker) - 1
            ]
            new_files.append(
                jevrev.state.FileContent(
                    file=name,
                    start=1,
                    end=max(len(lines), 1),
                    name=jevrev.state.ContentKind.FILE,
                    kind=jevrev.state.ContentKind.FILE,
                    context="",
                    text="\n".join(lines),
                    sets=review.sets,
                )
            )
        states += [
            state.model_copy(
                update={"evaluated": jevrev.state.Evaluation.FILE}
            )
            for state in _file_diffs(hunks) + new_files
        ]
    if jevrev.state.Evaluation.HUNK in evaluations:
        new = [
            content
            for name, source in untracked.items()
            for content in _file_contents(
                name=name,
                source=source,
                review=_review_of(name=name, source=source, sets=sets),
            )
        ]
        states += [
            state.model_copy(
                update={
                    "headless": True,
                    "evaluated": jevrev.state.Evaluation.HUNK,
                }
            )
            for state in hunks + _merge_constants(new)
        ]
    return states


def _all_files(root, targets):
    """The files under each of `targets`, or each target itself, by path from `root`. Symlinks
    under a target are left out, so no file outside `root` is reached."""
    stack = []
    files = set()
    for target in targets:
        if target.is_file():
            files.add(str(target.relative_to(root)))
        else:
            stack.append(target)
    while stack:
        for path in stack.pop().iterdir():
            if path.is_symlink():
                continue
            if path.is_dir():
                stack.append(path)
            elif path.is_file():
                files.add(str(path.relative_to(root)))
    return sorted(files)


def _findings(state, root, answers: list[jevrev.jev.api.NoulAnswer], rule_set):
    """The rules of `rule_set` that jev's answers for `state` meet its error threshold on, as findings."""
    parsed = _read_set(rule_set)
    for position, rule in enumerate(parsed.rules, 1):
        name = jevrev.jev.ask.question_name(position)
        answer = next((a for a in answers if a.question == name), None)
        if answer is None or answer.probability < parsed.error_threshold:
            continue
        yield Finding(
            file=str(root / state.file),
            start=state.start,
            end=state.end,
            item=state.name,
            rule=f"{rule_set.name}#{position}",
            probability=round(answer.probability, 2),
            flags=rule.flags,
            violation=" ".join(rule.bad),
            accepted=" ".join(rule.good),
            evaluated=state.evaluated,
        )


async def _ask(state, rule_set, gate, verbose, client, model):
    async with gate:
        try:
            request = jevrev.jev.ask.build_request(
                questions=jevrev.jev.ask.load_questions(rule_set),
                state=state.render(),
                model=model,
            )
        except jevrev.jev.ask.UserError as e:
            raise AskFailed(state, str(e))
        except jevrev.jev.ask.Rejected as e:
            raise AskFailed(state, "\n".join(jevrev.jev.ask.rejected_lines(e)))
        if verbose:
            print(
                json_dumps(
                    {
                        "file": state.file,
                        "start": state.start,
                        "item": state.name,
                        "evaluated": state.evaluated,
                        "request": request.model_dump(
                            mode="json", exclude_none=True
                        ),
                    }
                ),
                file=sys.stderr,
            )
        try:
            answers = await client.ask(request)
        except jevrev.jev.api.UserError as e:
            raise AskFailed(state, str(e))
    return answers


async def scan_states(
    rules,
    root=None,
    paths=(),
    extra_rules=(),
    rule_set=None,
    base=None,
    evaluate=None,
    context=None,
    all_added=False,
):
    """The resolved root, and the states of it to ask jev about, as main's
    flags of the same names select them."""
    evaluations = evaluate or [jevrev.state.Evaluation.HUNK]

    root = (await git.current_repository() if root is None else root).resolve()
    if not root.is_dir():
        raise UserError(f"--root {root} is not a directory")
    sets = _sets_under(
        tuple(
            {
                found.path: found
                for directory in (rules, *extra_rules)
                for found in _rule_sets(directory.resolve())
            }.values()
        ),
        root,
    )
    targets = []
    for path in paths:
        if path.is_absolute():
            raise UserError(
                f"{path} is absolute; give PATHs relative to --root"
            )
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise UserError(f"{path} is outside --root {root}")
        if not target.is_file() and not target.is_dir():
            raise UserError(
                f"{path} is neither a directory nor a file in {root}"
            )
        targets.append(target)
    targets = targets or [root]
    pathspecs = [str(target.relative_to(root)) for target in targets]
    # Files named on the command line are reviewed whatever their language.
    named = {
        str(target.relative_to(root)) for target in targets if target.is_file()
    }

    if base is None and (
        evaluate is not None or context is not None or all_added
    ):
        raise UserError("--evaluate, --context and --all-added need --base")
    if not await git.in_repository(root):
        # Outside a repository every file is new: reviewed whole, or as added with --base.
        hunks, whole = [], _all_files(root, targets)
    elif base is None:
        whole = await git.tracked_files(root, pathspecs)
        if ignored := sorted(named - set(whole)):
            raise UserError(f"{', '.join(ignored)} ignored by git")
    else:
        lines = _DEFAULT_CONTEXT if context is None else context
        hunks, whole = await _changed_hunks(
            root=root,
            pathspecs=pathspecs,
            base=base,
            context=(f"-U{lines}",),
            sets=sets,
        )
    sources = {}
    for name in whole:
        # git lists an untracked nested repository as its directory. A symlink out of the root
        # would send a file outside it to jev.
        if not (root / name).is_file() or not _inside(root, root / name):
            continue
        source = (root / name).read_bytes()
        try:
            source.decode()
        except UnicodeDecodeError:
            # Not text, so nothing for the rule sets to judge.
            continue
        if (
            base is None
            and name not in named
            and not _in_whole_scan(name, source)
        ):
            continue
        sources[name] = source
    if base is None:
        scanned = _merge_constants(
            [
                state
                for name, source in sources.items()
                for state in _file_contents(
                    name=name,
                    source=source,
                    review=_review_of(name=name, source=source, sets=sets),
                )
            ]
        )
    else:
        scanned = _change_states(
            hunks=hunks,
            untracked=sources,
            evaluations=evaluations,
            all_added=all_added,
            sets=sets,
        )
    scanned = [
        state.model_copy(
            update={"sets": (rule_set,) if rule_set else state.sets}
        )
        for state in scanned
        if rule_set or state.sets
    ]
    return root, scanned


async def review(
    scanned,
    root,
    concurrency,
    verbose,
    client: jevrev.jev.api.Client,
    model: str,
):
    """The findings jev, asked through client with model, reports on scanned,
    and the requests that failed."""
    asked = [(state, s) for state in scanned for s in state.sets]
    gate = asyncio.Semaphore(concurrency)
    results = await asyncio.gather(
        *(
            _ask(
                state=state,
                rule_set=s,
                gate=gate,
                verbose=verbose,
                client=client,
                model=model,
            )
            for state, s in asked
        ),
        return_exceptions=True,
    )
    if asked and all(isinstance(result, AskFailed) for result in results):
        raise UserError(f"every jev request failed; the first: {results[0]}")
    found = []
    failures = []
    # A rule two of a state's sets both ask, word for word, is reported once, from the first set.
    reported = set()
    for (state, rule_set), result in zip(asked, results):
        if isinstance(result, AskFailed):
            failures.append(f"{rule_set.name}: {result}")
            continue
        if isinstance(result, BaseException):
            raise result
        for finding in _findings(
            state=state, root=root, answers=result, rule_set=rule_set
        ):
            if (id(state), finding.flags) in reported:
                continue
            reported.add((id(state), finding.flags))
            found.append(finding)
    return _without_file_repeats(found), failures


def summary_line(scanned, root, found, failures):
    return (
        f"{len(scanned)} states reviewed in {root}, {len(found)} findings, "
        f"{len(failures)} failures"
    )


def _without_file_repeats(found):
    """`found` without each file finding whose rule also fired on a hunk of the same file,
    as the hunk's finding names the lines."""
    in_hunks = {
        (finding.file, finding.flags)
        for finding in found
        if finding.evaluated == jevrev.state.Evaluation.HUNK
    }
    return [
        finding
        for finding in found
        if finding.evaluated != jevrev.state.Evaluation.FILE
        or (finding.file, finding.flags) not in in_hunks
    ]

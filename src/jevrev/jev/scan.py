"""Scan text files with jev rule sets. A rule set's `when` selects the files it reviews, and
nothing else. Each selected file is reviewed whole; with a change, also each of its hunks and all of
them together; and, for each of its languages that parses it, each range of the file that language
yields: every range, or with a change the ranges it touches. Every rule set that selects a file is
asked about all of its states."""

import asyncio
import collections.abc
import dataclasses
import functools
import json
import logging
import pathlib
import sys
import typing

import pathspec
import pydantic
import pydantic.dataclasses
import yaml

import jevrev.files
import jevrev.git
import jevrev.jev.api
import jevrev.jev.ask
import jevrev.languages
import jevrev.languages.base
import jevrev.rules
import jevrev.state

type Language = type[jevrev.languages.base.Language]

_log = logging.getLogger(__name__)


@functools.cache
def _read_set(path):
    try:
        return jevrev.rules.RuleSet.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, pydantic.ValidationError) as e:
        raise UserError(f"rule set {path}: {e}") from e


@pydantic.dataclasses.dataclass(frozen=True)
class ScanTargetFile:
    """A file a scan considers: its path from the root, and the languages its name or first line
    marks it in."""

    path: jevrev.files.SourcePath
    languages: frozenset[Language]


@dataclasses.dataclass(frozen=True)
class Condition:
    """One `when` entry; an empty field matches every file."""

    languages: frozenset[jevrev.rules.Language]
    patterns: pathspec.GitIgnoreSpec | None
    # Each matches at least one path of the source.
    exists: tuple[pathspec.Pattern, ...]

    def holds_among(self, paths):
        return all(
            next(pathspec.GitIgnoreSpec([pattern]).match_files(paths), None)
            is not None
            for pattern in self.exists
        )

    def matches(self, file: ScanTargetFile) -> bool:
        return (
            not self.languages
            or any(
                language.name in self.languages for language in file.languages
            )
        ) and (self.patterns is None or self.patterns.match_file(file.path))


@dataclasses.dataclass(frozen=True)
class RuleSet:
    path: pathlib.Path
    # The set reviews a file when any of these matches it.
    conditions: tuple[Condition, ...]

    def among(self, paths):
        """The set as it applies to a source of `paths`: only the conditions whose paths exist
        among them."""
        return dataclasses.replace(
            self,
            conditions=tuple(
                c for c in self.conditions if c.holds_among(paths)
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


def _sets_among(sets, paths):
    """The rule sets that can apply to a source of `paths`, each with only its conditions that
    do."""
    return tuple(s for s in (s.among(paths) for s in sets) if s.conditions)


@dataclasses.dataclass(frozen=True)
class Review:
    """A file and the rule sets whose `when` selects it."""

    file: ScanTargetFile
    sets: tuple[pathlib.Path, ...]

    def __post_init__(self):
        if not self.sets:
            raise ValueError(f"no rule set selects {self.file.path}")


def _review_of(file: ScanTargetFile, sets) -> Review | None:
    """file's review by the rule sets of sets that select it; None when none does."""
    selecting = tuple(
        rule_set.path
        for rule_set in sets
        if any(c.matches(file) for c in rule_set.conditions)
    )
    return Review(file=file, sets=selecting) if selecting else None


class UserError(Exception):
    pass


def _text(file: ScanTargetFile, source: bytes) -> str | None:
    """source as text; None, logged at debug, when it is not UTF-8."""
    try:
        return source.decode()
    except UnicodeDecodeError as e:
        _log.debug("skipping %s: not UTF-8 text: %s", file.path, e)
        return None


@dataclasses.dataclass(frozen=True)
class _Before:
    """A file before a change, rebuilt from the file after it and the change's hunks."""

    text: str
    # Each line the change keeps, by its number before the change, to its number after it.
    kept: dict[int, int]

    @classmethod
    def of(cls, after: list[str], hunks: list[jevrev.git.Hunk]) -> "_Before":
        lines: list[str] = []
        kept: dict[int, int] = {}
        next_after = 1

        def keep_until(stop):
            nonlocal next_after
            while next_after < stop:
                lines.append(after[next_after - 1])
                kept[len(lines)] = next_after
                next_after += 1

        for hunk in hunks:
            # A hunk with no lines after the change sits after its target_start.
            keep_until(
                hunk.target_start
                if hunk.target_length
                else hunk.target_start + 1
            )
            for line in hunk.lines:
                match line:
                    case jevrev.git.ContextLine():
                        lines.append(line.text)
                        kept[len(lines)] = line.number
                        next_after = line.number + 1
                    case jevrev.git.RemovedLine():
                        lines.append(line.text)
                    case jevrev.git.AddedLine():
                        next_after = line.number + 1
        keep_until(len(after) + 1)
        return cls(text="".join(f"{line}\n" for line in lines), kept=kept)


def _changed_ranges(
    language: Language,
    after: list[jevrev.languages.base.LineRange],
    text: str,
    change: jevrev.git.ChangedFile,
) -> list[jevrev.languages.base.LineRange]:
    """The ranges of after, language's ranges of text, that change touches: each holding an added
    line, or a line kept from a range before the change that lost a line. Every range when the
    file before the change is not valid in language."""
    before = _Before.of(text.splitlines(), change.hunks)
    try:
        before_ranges = language.states(before.text)
    except jevrev.languages.base.ParseError:
        _log.info(
            "%s: the old side does not parse as %s, so every range counts as touched",
            change.path,
            language.name,
        )
        return after
    touched = {
        line.number
        for hunk in change.hunks
        for line in hunk.lines
        if isinstance(line, jevrev.git.AddedLine)
    }
    for before_range in before_ranges:
        if any(n not in before.kept for n in before_range.lines):
            touched.update(
                before.kept[n] for n in before_range.lines if n in before.kept
            )
    return [r for r in after if not touched.isdisjoint(r.lines)]


def _test_cutoff(lines: list[str], marker: str | None) -> int:
    """The number of the first line that is marker, past the last line when there is none."""
    return next(
        (n for n, line in enumerate(lines, 1) if line == marker),
        len(lines) + 1,
    )


def _language_states(
    review: Review,
    language: Language,
    text: str,
    change: jevrev.git.ChangedFile | None,
) -> list[jevrev.state.LanguageState]:
    """The states of language's ranges of the file, before its test code: every range, or with a
    change only those it touches. None when text is not valid in language."""
    try:
        ranges = language.states(text)
    except jevrev.languages.base.ParseError:
        _log.info(
            "%s: does not parse as %s, so it has no %s states",
            review.file.path,
            language.name,
            language.name,
        )
        return []
    if change is not None:
        ranges = _changed_ranges(language, ranges, text, change)
    lines = text.splitlines()
    cutoff = _test_cutoff(lines, language.test_marker)
    states = []
    for r in ranges:
        if r.start >= cutoff:
            continue
        end = min(r.end, cutoff - 1)
        part = lines[r.start - 1 : end]
        # Blank lines between definitions hold nothing to judge.
        if not any(line.strip() for line in part):
            continue
        states.append(
            jevrev.state.LanguageState(
                file=review.file.path,
                start=r.start,
                end=end,
                sets=review.sets,
                text="\n".join(part),
                language=language.name,
            )
        )
    return states


def _hunk_states(
    review: Review, change: jevrev.git.ChangedFile
) -> list[jevrev.state.State]:
    """A state of each hunk of change, and one of all of them together."""
    states = []
    for hunk in change.hunks:
        header = (
            f"@@ -{hunk.source_start},{hunk.source_length} "
            f"+{hunk.target_start},{hunk.target_length} @@ {hunk.section}"
        )
        # A hunk that leaves no lines sits after its target_start, which is 0 at the top.
        start = max(hunk.target_start, 1)
        states.append(
            jevrev.state.HunkState(
                file=review.file.path,
                start=start,
                end=max(hunk.target_start + hunk.target_length - 1, start),
                sets=review.sets,
                hunk=jevrev.state.DiffHunk(
                    header=header.rstrip(),
                    lines=[f"{line.prefix}{line.text}" for line in hunk.lines],
                ),
            )
        )
    if not states:
        return []
    return [
        *states,
        jevrev.state.HunksState(
            file=review.file.path,
            start=states[0].start,
            end=states[-1].end,
            sets=review.sets,
            hunks=[state.hunk for state in states],
        ),
    ]


def _states(
    review: Review, text: str, change: jevrev.git.ChangedFile | None
) -> list[jevrev.state.State]:
    """Every state of a reviewed file of text: the whole file, its change's hunks when it has a
    change, and the ranges of each of its languages."""
    return [
        jevrev.state.FileState(
            file=review.file.path,
            start=1,
            end=max(len(text.splitlines()), 1),
            sets=review.sets,
            text=text,
        ),
        *(_hunk_states(review, change) if change is not None else []),
        *(
            state
            for language in sorted(
                review.file.languages, key=lambda language: language.name
            )
            for state in _language_states(review, language, text, change)
        ),
    ]


# The rule set's file name and the rule's position in it, from 1, such as text.yaml#1.
type RuleId = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"^[^#]+#[1-9][0-9]*$")
]

# How likely jev judges the rule broken, rounded to 2 places.
type Probability = typing.Annotated[float, pydantic.Field(ge=0.0, le=1.0)]

# What a rule flags, as its rule set states it.
type RuleFlags = str

# A rule's bad or good examples, joined by spaces.
type RuleExamples = str


class Finding(pydantic.BaseModel):
    file: jevrev.files.SourcePath
    start: jevrev.files.LineNumber
    end: jevrev.files.LineNumber
    rule: RuleId
    probability: Probability
    flags: RuleFlags
    # The rule's examples of what breaks it.
    violation: RuleExamples
    # The rule's examples of what keeps it.
    accepted: RuleExamples
    state: jevrev.state.StateKind


class AskFailed(Exception):
    def __init__(self, state, stderr):
        super().__init__(state, stderr)
        self.state = state
        self.stderr = stderr

    def __str__(self):
        return (
            f"{self.state.file}:{self.state.start}-{self.state.end} "
            f"{self.state.kind}: {self.stderr.strip()}"
        )


def json_dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


# The context lines around each change of a diff a scan reviews.
DIFF_CONTEXT = 20


def _findings(state, answers: list[jevrev.jev.api.NoulAnswer], rule_set):
    parsed = _read_set(rule_set)
    for position, rule in enumerate(parsed.rules, 1):
        name = jevrev.jev.ask.question_name(position)
        answer = next((a for a in answers if a.question == name), None)
        if answer is None or answer.probability < parsed.error_threshold:
            continue
        yield Finding(
            file=state.file,
            start=state.start,
            end=state.end,
            rule=f"{rule_set.name}#{position}",
            probability=round(answer.probability, 2),
            flags=rule.flags,
            violation=" ".join(rule.bad),
            accepted=" ".join(rule.good),
            state=state.kind,
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
                        "end": state.end,
                        "state": state.kind,
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


@pydantic.dataclasses.dataclass(frozen=True)
class _Unchanged:
    """A file of a scan without a diff."""

    path: jevrev.files.SourcePath

    def change(self, text: str) -> None:
        return None


@dataclasses.dataclass(frozen=True)
class _Changed:
    """A file a diff changes."""

    changed: jevrev.git.ChangedFile

    @property
    def path(self) -> jevrev.files.SourcePath:
        return self.changed.path

    def change(self, text: str) -> jevrev.git.ChangedFile:
        return self.changed


@pydantic.dataclasses.dataclass(frozen=True)
class _Untracked:
    """A file git does not track, which a diff to the files on disk adds whole."""

    path: jevrev.files.SourcePath

    def change(self, text: str) -> jevrev.git.ChangedFile:
        return jevrev.git.ChangedFile.added(self.path, text)


async def scan_states(
    rules,
    files: jevrev.files.Files,
    ignore: jevrev.files.Ignore | None,
    diff: jevrev.git.Diff | None = None,
    untracked: collections.abc.Sequence[str] = (),
    include: collections.abc.Sequence[str] = (),
    exclude: collections.abc.Sequence[str] = (),
) -> list[jevrev.state.State]:
    """The states to ask jev about. `files` holds the reviewed version, and every path is
    relative to it. Without a diff, every file of `files` is a scan target; with one, each file it
    changes, and each of `untracked`, as added whole. With `ignore`, every path it matches is left
    out; None (--no-ignore) leaves none out. Of the paths `ignore` leaves, only those an `include`
    pattern matches are scan targets, or every one when there are none, and none an `exclude`
    pattern matches; both are gitignore patterns relative to `files`. A rule set's `exists` still
    sees every path `ignore` leaves."""
    paths = await files.paths()
    if ignore is not None:
        paths = [path for path in paths if not ignore.matches(path)]
    included = pathspec.GitIgnoreSpec.from_lines(include)
    excluded = pathspec.GitIgnoreSpec.from_lines(exclude)
    scope = frozenset(
        path
        for path in paths
        if (not include or included.match_file(path))
        and not excluded.match_file(path)
    )
    sets = _sets_among(
        tuple(
            {
                found.path: found
                for directory in rules
                for found in _rule_sets(directory.resolve())
            }.values()
        ),
        paths,
    )
    targets: collections.abc.Sequence[_Unchanged | _Changed | _Untracked] = (
        [_Unchanged(path) for path in paths]
        if diff is None
        else [_Changed(changed) for changed in diff.root]
        + [_Untracked(path) for path in untracked]
    )
    states = []
    for target in targets:
        # A submodule's diff names its directory, and an ignored file is out of scope.
        if target.path not in scope:
            continue
        source = await files.read(target.path)
        file = ScanTargetFile(
            path=target.path,
            languages=jevrev.languages.detect(target.path, source),
        )
        review = _review_of(file, sets)
        if review is None:
            continue
        text = _text(file, source)
        if text is None:
            continue
        states += _states(review, text, target.change(text))
    return states


async def review(
    scanned,
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
            state=state, answers=result, rule_set=rule_set
        ):
            if (id(state), finding.flags) in reported:
                continue
            reported.add((id(state), finding.flags))
            found.append(finding)
    return _without_file_repeats(found), failures


def summary_line(scanned, source, found, failures):
    """The scan's summary; source names what was scanned."""
    return (
        f"{len(scanned)} states reviewed in {source}, {len(found)} findings, "
        f"{len(failures)} failures"
    )


def _without_file_repeats(found):
    """`found` without each finding on a whole file or change whose rule also fired on a hunk or
    range of the same file, as that finding names the lines."""
    on_lines = {
        (finding.file, finding.flags)
        for finding in found
        if finding.state in jevrev.state.LINE_KINDS
    }
    return [
        finding
        for finding in found
        if finding.state in jevrev.state.LINE_KINDS
        or (finding.file, finding.flags) not in on_lines
    ]

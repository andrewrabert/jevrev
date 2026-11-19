"""Scan code and text with jev rule sets: each definition, run of top-level code or whole file of
the files in a known language of one version of a source, or the hunks of any text file a diff
changes. Each file is reviewed against the rule sets under the rules directory whose `when`
matches it."""

import asyncio
import collections.abc
import dataclasses
import enum
import functools
import json
import os
import pathlib
import re
import sys
import typing

import pathspec
import pydantic
import yaml

import jevrev.files
import jevrev.git
import jevrev.jev.api
import jevrev.jev.ask
import jevrev.languages
import jevrev.languages.base
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
    # Each matches at least one path of the source.
    exists: tuple[pathspec.Pattern, ...]

    def holds_among(self, paths):
        return all(
            next(pathspec.GitIgnoreSpec([pattern]).match_files(paths), None)
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


def env_path(name):
    value = os.environ.get(name)
    return pathlib.Path(value) if value else None


def path_list(value):
    """The paths of a list separated by os.pathsep, as in PATH, without its empty entries."""
    return [pathlib.Path(entry) for entry in value.split(os.pathsep) if entry]


def _sets_among(sets, paths):
    """The rule sets that can apply to a source of `paths`, each with only its conditions that
    do."""
    return tuple(s for s in (s.among(paths) for s in sets) if s.conditions)


@dataclasses.dataclass(frozen=True)
class Review:
    """How a file is reviewed."""

    # The rule sets every state of the file is reviewed against.
    sets: tuple[pathlib.Path, ...]
    # How the file is split into states and where its test code starts.
    language: type[jevrev.languages.base.Language]


def _review_of(name, source, sets):
    """How a file is reviewed against `sets`; `name` is its path in the source."""
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
    """Whether a scan without a diff reviews the file: it has a language. With a diff, every
    changed text file is reviewed."""
    return jevrev.languages.detect(name, source).name is not None


class UserError(Exception):
    pass


type LineNumber = typing.Annotated[int, pydantic.Field(ge=1)]

# The path of a finding's file, relative to the source.
type FindingPath = typing.Annotated[
    str, pydantic.StringConstraints(min_length=1)
]

# The definition, run of code, hunk or file a finding is in, as its state names it.
type ItemName = str

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
    file: FindingPath
    start: LineNumber
    end: LineNumber
    item: ItemName
    rule: RuleId
    probability: Probability
    flags: RuleFlags
    # The rule's examples of what breaks it.
    violation: RuleExamples
    # The rule's examples of what keeps it.
    accepted: RuleExamples
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


async def _changed_hunks(files, diff, present, sets):
    """Each hunk of `diff` that adds a line before its file's tests, as a diff of its file, its
    file read from `files`; a file not in `present` is left out."""
    changed = []
    for changed_file in diff.root:
        name = changed_file.path
        # A submodule's diff names its directory, and an ignored file is not present.
        if name not in present:
            continue
        source = await files.read(name)
        review = _review_of(name=name, source=source, sets=sets)
        cutoff = _test_cutoff(source, review.language.test_marker)
        for hunk in changed_file.hunks:
            lines = [line for line in hunk.lines if line.number < cutoff]
            if not any(line.added for line in lines):
                continue
            header = (
                f"@@ -{hunk.source_start},{hunk.source_length} "
                f"+{hunk.target_start},{hunk.target_length} @@ {hunk.section}"
            )
            changed.append(
                jevrev.state.FileDiff(
                    file=name,
                    start=hunk.target_start,
                    end=lines[-1].number,
                    name=hunk.section or jevrev.state.Evaluation.HUNK,
                    sets=review.sets,
                    hunks=[
                        jevrev.state.DiffHunk(
                            header=header.rstrip(),
                            lines=[
                                f"{'+' if line.added else ' '}{line.text}"
                                for line in lines
                            ],
                        )
                    ],
                )
            )
    return changed


# The context lines around each change of a diff a scan reviews.
DIFF_CONTEXT = 20


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


async def _texts(files, names, keep):
    texts = {}
    for name in names:
        source = await files.read(name)
        try:
            source.decode()
        except UnicodeDecodeError:
            continue
        if keep(name, source):
            texts[name] = source
    return texts


async def scan_states(
    rules,
    files: jevrev.files.Files,
    ignore: jevrev.files.Ignore | None,
    diff: jevrev.git.Diff | None = None,
    untracked: collections.abc.Sequence[str] = (),
    extra_rules=(),
    rule_set=None,
    evaluate=None,
    all_added=False,
) -> list[jevrev.state.State]:
    """The states to ask jev about. `files` holds the reviewed version, and every path is
    relative to it. Without a diff, every file of `files` is reviewed whole; with one, its hunks
    are, their files read from `files`, and each of `untracked` whole, as added. With `ignore`,
    every path it matches is left out, from files' paths, diff's files and untracked alike;
    None (--no-ignore) leaves none out."""
    evaluations = evaluate or [jevrev.state.Evaluation.HUNK]
    if diff is None and (evaluate is not None or all_added):
        raise UserError("evaluate and all_added need a diff")
    paths = await files.paths()
    if ignore is not None:
        paths = [path for path in paths if not ignore.matches(path)]
    present = set(paths)
    sets = _sets_among(
        tuple(
            {
                found.path: found
                for directory in (rules, *extra_rules)
                for found in _rule_sets(directory.resolve())
            }.values()
        ),
        paths,
    )
    if diff is None:
        sources = await _texts(files, paths, _in_whole_scan)
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
            hunks=await _changed_hunks(
                files=files, diff=diff, present=present, sets=sets
            ),
            untracked=await _texts(
                files,
                [name for name in untracked if name in present],
                lambda name, source: True,
            ),
            evaluations=evaluations,
            all_added=all_added,
            sets=sets,
        )
    return [
        state.model_copy(
            update={"sets": (rule_set,) if rule_set else state.sets}
        )
        for state in scanned
        if rule_set or state.sets
    ]


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

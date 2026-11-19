"""What every language declares. Each language is a subclass, never instantiated."""

import dataclasses
import re
import typing

import pydantic
import tree_sitter

import jevrev.files
import jevrev.rules


class ParseError(Exception):
    """The text is not valid in the language."""


class LineRange(pydantic.BaseModel):
    """Lines start to end of a file, both included."""

    model_config = pydantic.ConfigDict(frozen=True)

    start: jevrev.files.LineNumber
    end: jevrev.files.LineNumber

    @pydantic.model_validator(mode="after")
    def _ordered(self):
        if self.end < self.start:
            raise ValueError(f"{self.start}-{self.end} is not a line range")
        return self

    @property
    def lines(self) -> range:
        return range(self.start, self.end + 1)


@dataclasses.dataclass(frozen=True)
class Unit:
    """Lines first to last of a text, numbered from 0, that a language yields as one range;
    adjacent mergeable units are one range."""

    first: int
    last: int
    mergeable: bool = False


def tile(units: typing.Iterable[Unit], line_count: int) -> list[LineRange]:
    """The ranges of a text of line_count lines: each run of units, adjacent mergeable units
    at most one line apart joined, and each run of lines between them, so the ranges cover
    every line once."""
    joined: list[Unit] = []
    for unit in sorted(units, key=lambda u: u.first):
        previous = joined[-1] if joined else None
        if (
            previous is not None
            and previous.mergeable
            and unit.mergeable
            and unit.first - previous.last <= 2
        ):
            joined[-1] = Unit(previous.first, unit.last, mergeable=True)
        else:
            joined.append(unit)
    ranges = []
    line = 0
    for unit in joined:
        if unit.first > line:
            ranges.append(LineRange(start=line + 1, end=unit.first))
        ranges.append(LineRange(start=unit.first + 1, end=unit.last + 1))
        line = unit.last + 1
    if line < line_count:
        ranges.append(LineRange(start=line + 1, end=line_count))
    return ranges


def leading(node: tree_sitter.Node, kinds) -> list[tree_sitter.Node]:
    """The siblings of `kinds` directly above `node`, with no blank line between, top first."""
    found = []
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in kinds:
        below = found[0] if found else node
        if below.start_point[0] - sibling.end_point[0] > 1:
            break
        found.insert(0, sibling)
        sibling = sibling.prev_sibling
    return found


class Language:
    """A language whose files are one range."""

    # The name a rule set's `when.language` gives the language.
    name: typing.ClassVar[jevrev.rules.Language]
    # File name suffixes, such as `.rs`, that mark a file in the language.
    suffixes: typing.ClassVar[tuple[str, ...]] = ()
    # Whole file names, such as `justfile`, that mark a file in the language.
    file_names: typing.ClassVar[tuple[str, ...]] = ()
    # A first line that matches one of these marks a script in the language.
    shebangs: typing.ClassVar[tuple[re.Pattern[str], ...]] = ()
    # A line marking where test code starts; no range reaches it or past it.
    test_marker: typing.ClassVar[str | None] = None

    @classmethod
    def states(cls, text: str) -> list[LineRange]:
        """The line ranges of text, which cover every line once, in order; ParseError when text
        is not valid in the language."""
        return tile([], len(text.splitlines()))

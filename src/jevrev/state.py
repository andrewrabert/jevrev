"""What one jev request asks about: a state, of one file, rendered as a diff."""

import enum
import pathlib
import typing

import pydantic

import jevrev.files
import jevrev.rules


class StateKind(enum.StrEnum):
    """What of a file a state holds."""

    # The whole file.
    FILE = "file"
    # One hunk of a change to the file.
    HUNK = "hunk"
    # Every hunk of a change to the file.
    HUNKS = "hunks"
    # One range of the file that a language yields.
    LANGUAGE = "language"


# The kinds of states that hold a few lines of a file, which a finding on one points at; the other
# kinds hold a whole file or change.
LINE_KINDS = frozenset({StateKind.HUNK, StateKind.LANGUAGE})


class DiffHunk(pydantic.BaseModel):
    header: str
    # The hunk's lines, each with its `+`, `-` or ` ` prefix.
    lines: list[str]


class State(pydantic.BaseModel):
    """Lines of one file that jev is asked about, and the rule sets it is asked against."""

    model_config = pydantic.ConfigDict(frozen=True)

    kind: typing.ClassVar[StateKind]
    # Whether the rendered diff leaves out its `@@` lines.
    headless: typing.ClassVar[bool] = False

    file: jevrev.files.SourcePath
    start: jevrev.files.LineNumber
    end: jevrev.files.LineNumber
    # The rule sets the state is reviewed against.
    sets: typing.Annotated[
        tuple[pathlib.Path, ...], pydantic.Field(min_length=1)
    ]

    def _hunks(self) -> list[DiffHunk]:
        raise NotImplementedError

    def render(self) -> str:
        """The state as jev is given it: a diff of the file."""
        return "\n".join(
            [
                f"diff --git a/{self.file} b/{self.file}",
                f"--- a/{self.file}",
                f"+++ b/{self.file}",
                *(
                    line
                    for hunk in self._hunks()
                    for line in (
                        hunk.lines
                        if self.headless
                        else [hunk.header, *hunk.lines]
                    )
                ),
            ]
        )


class _Text(State):
    """Lines of the file as it is, every one rendered as added."""

    text: str

    def _hunks(self):
        lines = self.text.splitlines()
        return [
            DiffHunk(
                header=f"@@ -0,0 +{self.start},{len(lines)} @@",
                lines=[f"+{line}" for line in lines],
            )
        ]


class FileState(_Text):
    kind = StateKind.FILE


class LanguageState(_Text):
    kind = StateKind.LANGUAGE

    language: jevrev.rules.Language


class HunkState(State):
    kind = StateKind.HUNK
    headless = True

    hunk: DiffHunk

    def _hunks(self):
        return [self.hunk]


class HunksState(State):
    kind = StateKind.HUNKS

    hunks: typing.Annotated[list[DiffHunk], pydantic.Field(min_length=1)]

    def _hunks(self):
        return self.hunks

"""What one jev request asks about: a state, of one file, rendered as a diff."""

import enum
import pathlib

import pydantic


class Evaluation(enum.StrEnum):
    """What a state of a change evaluates: one hunk, or all of a file's hunks."""

    FILE = "file"
    HUNK = "hunk"


class ContentKind(enum.StrEnum):
    """What a FileContent is; MERGED kinds next to each other are reviewed as one state."""

    CONST = "const"
    STATIC = "static"
    ASSIGNMENT = "assignment"
    DEFINITION = "definition"
    TOP_LEVEL = "top-level"
    FILE = "file"


MERGED = frozenset(
    {ContentKind.CONST, ContentKind.STATIC, ContentKind.ASSIGNMENT}
)


class DiffHunk(pydantic.BaseModel):
    header: str
    # The hunk's added and context lines, each with its `+` or ` ` prefix; removed lines are left
    # out.
    lines: list[str]


class State(pydantic.BaseModel):
    """Lines of one file that jev is asked about, and the rule sets it is asked against."""

    # The file's path from the root.
    file: str
    start: int
    end: int
    name: str
    # The rule sets the state is reviewed against.
    sets: tuple[pathlib.Path, ...] = ()
    # How much of a change the state evaluates; None outside a change.
    evaluated: Evaluation | None = None
    # Whether the rendered diff leaves out its `@@` lines.
    headless: bool = False

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


class FileContent(State):
    """A file's current content, or part of it, read from disk; every line is rendered as
    added."""

    kind: ContentKind = ContentKind.DEFINITION
    # What the content is inside, such as its impl or class; empty at the top level.
    context: str
    text: str

    def _hunks(self):
        lines = self.text.splitlines()
        return [
            DiffHunk(
                header=f"@@ -0,0 +{self.start},{len(lines)} @@ {self.context}".rstrip(),
                lines=[f"+{line}" for line in lines],
            )
        ]


class FileDiff(State):
    """git's diff of one file since the base: one of its hunks, or all of them."""

    hunks: list[DiffHunk]

    def _hunks(self):
        return self.hunks

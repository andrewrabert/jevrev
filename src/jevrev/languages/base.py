"""What every language declares. Each language is a subclass, never instantiated."""

import re
import typing

import jevrev.rules
import jevrev.state


class Language:
    """A file in no language: the whole file is one state."""

    # The name a rule set's `when.language` gives the language; None for no language.
    name: typing.ClassVar[jevrev.rules.Language | None] = None
    # File name suffixes, such as `.rs`, that mark a file in the language.
    suffixes: typing.ClassVar[tuple[str, ...]] = ()
    # Whole file names, such as `justfile`, that mark a file in the language.
    file_names: typing.ClassVar[tuple[str, ...]] = ()
    # A first line that matches one of these marks a script in the language.
    shebangs: typing.ClassVar[tuple[re.Pattern[str], ...]] = ()
    # A line marking where test code starts; it and everything after it is left out.
    test_marker: typing.ClassVar[str | None] = None

    @classmethod
    def contents(cls, source, path) -> list[jevrev.state.FileContent]:
        """The parts of a file reviewed one at a time, from its source and its path from the root;
        the whole file is one."""
        lines = source.decode().splitlines()
        return [
            jevrev.state.FileContent(
                file=path,
                start=1,
                end=max(len(lines), 1),
                name=jevrev.state.ContentKind.FILE,
                kind=jevrev.state.ContentKind.FILE,
                context="",
                text="\n".join(lines),
            )
        ]

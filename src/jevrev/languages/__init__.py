"""The languages a rule set's `when.language` can name, each in its own module, and how a file's
languages are detected."""

import pathlib

from jevrev.languages import base, just, markdown, python, rust, shell

_ALL = (rust.Rust, python.Python, shell.Shell, just.Just, markdown.Markdown)


def detect(name: str, source: bytes) -> frozenset[type[base.Language]]:
    """The languages a file is in, by its name, its suffix or its first line; none when it is in
    no language."""
    path = pathlib.PurePath(name)
    first = source.split(b"\n", 1)[0].decode(errors="replace")
    return frozenset(
        language
        for language in _ALL
        if path.name in language.file_names
        or path.suffix in language.suffixes
        or any(pattern.search(first) for pattern in language.shebangs)
    )

"""The languages a rule set's `when.language` can name, each in its own module, and how a file's
language is detected."""

import pathlib

from jevrev.languages import base, just, markdown, python, rust, shell

# In the order their shebangs are tried.
_ALL = (rust.Rust, python.Python, shell.Shell, just.Just, markdown.Markdown)

_NAMES = {name: language for language in _ALL for name in language.file_names}
_SUFFIXES = {
    suffix: language for language in _ALL for suffix in language.suffixes
}


def detect(name, source) -> type[base.Language]:
    """The language of a file, or base.Language when it is in none. A file's name decides before
    its suffix, and its suffix before its shebang."""
    path = pathlib.PurePath(name)
    if path.name in _NAMES:
        return _NAMES[path.name]
    if path.suffix in _SUFFIXES:
        return _SUFFIXES[path.suffix]
    first = source.split(b"\n", 1)[0].decode(errors="replace")
    return next(
        (
            language
            for language in _ALL
            if any(pattern.search(first) for pattern in language.shebangs)
        ),
        base.Language,
    )

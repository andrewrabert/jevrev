"""POSIX sh: each function, and each run of top-level code between them. Every state carries the
script's top-level set and trap commands that lie outside it as context."""

import re

import tree_sitter
import tree_sitter_bash

import jevrev.rules
import jevrev.state
from jevrev.languages import base

_PARSER = tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))

# Siblings that belong to the command after them.
_LEADING = {"comment"}

# Top-level commands that change how every other part of the script runs.
_SETUP = {"set", "trap"}


def _leading(node, kinds):
    """The siblings of `kinds` directly above `node`, top first."""
    found = []
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in kinds:
        below = found[0] if found else node
        if below.start_point[0] - sibling.end_point[0] > 1:
            break
        found.insert(0, sibling)
        sibling = sibling.prev_sibling
    return found


def _contents(nodes, source, path):
    lines = source.decode().splitlines()
    # The shebang is not a comment for the rule sets to judge.
    first_line = 1 if lines and lines[0].startswith("#!") else 0
    setup = []
    for node in nodes:
        name = (
            node.child_by_field_name("name")
            if node.type == "command"
            else None
        )
        if (
            name is not None
            and source[name.start_byte : name.end_byte].decode() in _SETUP
        ):
            setup.append(node)

    def content(first, last, name, kind=jevrev.state.ContentKind.DEFINITION):
        start = max(
            (_leading(first, _LEADING) or [first])[0].start_point[0],
            first_line,
        )
        end = last.end_point[0]
        return jevrev.state.FileContent(
            file=path,
            start=start + 1,
            end=end + 1,
            name=name,
            kind=kind,
            context="; ".join(
                " ".join(source[n.start_byte : n.end_byte].decode().split())
                for n in setup
                if not start <= n.start_point[0] <= end
            ),
            text="\n".join(lines[start : end + 1]),
        )

    def top_level(run):
        return content(
            first=run[0],
            last=run[-1],
            name=jevrev.state.ContentKind.TOP_LEVEL,
            kind=jevrev.state.ContentKind.TOP_LEVEL,
        )

    found = []
    run = []
    for node in nodes:
        if node.type == "comment":
            continue
        if node.type != "function_definition":
            run.append(node)
            continue
        if run:
            found.append(top_level(run))
            run = []
        name = node.child_by_field_name("name")
        label = (
            source[name.start_byte : name.end_byte].decode() if name else ""
        )
        found.append(
            content(first=node, last=node, name=f"function {label}".strip())
        )
    if run:
        found.append(top_level(run))
    return found


class Shell(base.Language):
    name = jevrev.rules.Language.SH
    shebangs = (
        re.compile(r"^#!\S*/sh(\s|$)"),
        re.compile(r"^#!\S*/env\s+(-\S+\s+)*sh(\s|$)"),
    )

    @classmethod
    def contents(cls, source, path):
        return _contents(
            nodes=_PARSER.parse(source).root_node.named_children,
            source=source,
            path=path,
        )

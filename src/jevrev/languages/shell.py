"""POSIX sh: each function, and each run of top-level code between them."""

import enum
import re

import tree_sitter
import tree_sitter_bash

import jevrev.rules
from jevrev.languages import base

_PARSER = tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))


class _Node(enum.StrEnum):
    """The types of tree-sitter-bash nodes the ranges depend on."""

    COMMENT = "comment"
    FUNCTION_DEFINITION = "function_definition"


# Siblings that belong to the command after them.
_LEADING = frozenset({_Node.COMMENT})


def _units(nodes, text):
    # The shebang is not a comment for the rule sets to judge.
    after_shebang = 1 if text.startswith("#!") else 0

    def unit(first, last):
        return base.Unit(
            first=max(
                (base.leading(first, _LEADING) or [first])[0].start_point[0],
                after_shebang,
            ),
            last=last.end_point[0],
        )

    run = []
    for node in nodes:
        if node.type == _Node.COMMENT:
            continue
        if node.type != _Node.FUNCTION_DEFINITION:
            run.append(node)
            continue
        if run:
            yield unit(run[0], run[-1])
            run = []
        yield unit(node, node)
    if run:
        yield unit(run[0], run[-1])


class Shell(base.Language):
    name = jevrev.rules.Language.SH
    shebangs = (
        re.compile(r"^#!\S*/sh(\s|$)"),
        re.compile(r"^#!\S*/env\s+(-\S+\s+)*sh(\s|$)"),
    )

    @classmethod
    def states(cls, text):
        root = _PARSER.parse(text.encode(errors="surrogateescape")).root_node
        if root.has_error:
            raise base.ParseError("not valid sh")
        return base.tile(
            _units(root.named_children, text), len(text.splitlines())
        )

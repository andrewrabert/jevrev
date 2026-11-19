"""Python: each top-level statement, and each method of a class apart from its head; runs of
assignments are one range."""

import enum
import re

import tree_sitter
import tree_sitter_python

import jevrev.rules
from jevrev.languages import base

_PARSER = tree_sitter.Parser(
    tree_sitter.Language(tree_sitter_python.language())
)


class _Node(enum.StrEnum):
    """The types of tree-sitter-python nodes the ranges depend on."""

    COMMENT = "comment"
    IMPORT_STATEMENT = "import_statement"
    IMPORT_FROM_STATEMENT = "import_from_statement"
    FUTURE_IMPORT_STATEMENT = "future_import_statement"
    PASS_STATEMENT = "pass_statement"
    EXPRESSION_STATEMENT = "expression_statement"
    ASSIGNMENT = "assignment"
    CLASS_DEFINITION = "class_definition"
    FUNCTION_DEFINITION = "function_definition"


class _Field(enum.StrEnum):
    BODY = "body"
    # A decorated definition's definition, under its decorators.
    DEFINITION = "definition"


# Siblings that belong to the statement after them.
_LEADING = frozenset({_Node.COMMENT})

# Statements with nothing for the rule set to judge.
_SKIPPED = _LEADING | {
    _Node.IMPORT_STATEMENT,
    _Node.IMPORT_FROM_STATEMENT,
    _Node.FUTURE_IMPORT_STATEMENT,
    _Node.PASS_STATEMENT,
}


def _is_assignment(node):
    return (
        node.type == _Node.EXPRESSION_STATEMENT
        and bool(node.named_children)
        and node.named_children[0].type == _Node.ASSIGNMENT
    )


def _units(nodes):
    for node in nodes:
        if node.type in _SKIPPED:
            continue
        above = base.leading(node, _LEADING)
        first = (above[0] if above else node).start_point[0]
        definition = node.child_by_field_name(_Field.DEFINITION) or node
        body = definition.child_by_field_name(_Field.BODY)
        if definition.type == _Node.CLASS_DEFINITION and body is not None:
            methods = [
                c
                for c in body.named_children
                if (c.child_by_field_name(_Field.DEFINITION) or c).type
                == _Node.FUNCTION_DEFINITION
            ]
            if methods:
                head_end = (
                    base.leading(methods[0], _LEADING) or [methods[0]]
                )[0].start_point[0]
                yield base.Unit(first=first, last=head_end - 1)
                yield from _units(methods)
                continue
        yield base.Unit(
            first=first,
            last=node.end_point[0],
            mergeable=_is_assignment(definition),
        )


class Python(base.Language):
    name = jevrev.rules.Language.PYTHON
    suffixes = (".py",)
    shebangs = (
        re.compile(r"^#!\S*/python[\d.]*(\s|$)"),
        re.compile(r"^#!\S*/env\s+(-\S+\s+)*python[\d.]*(\s|$)"),
        re.compile(r"^#!/usr/bin/env.*uv.*run"),
    )

    @classmethod
    def states(cls, text):
        root = _PARSER.parse(text.encode(errors="surrogateescape")).root_node
        if root.has_error:
            raise base.ParseError("not valid Python")
        return base.tile(_units(root.named_children), text.splitlines())

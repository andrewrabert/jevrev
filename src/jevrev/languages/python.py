"""Python: each top-level statement, and each method of a class apart from its head."""

import re

import tree_sitter
import tree_sitter_python

import jevrev.rules
import jevrev.state
from jevrev.languages import base

_PARSER = tree_sitter.Parser(
    tree_sitter.Language(tree_sitter_python.language())
)

# Siblings that belong to the statement after them.
_LEADING = {"comment"}

# Statements with nothing for the rule set to judge.
_SKIPPED = {
    "import_statement",
    "import_from_statement",
    "future_import_statement",
    "comment",
    "pass_statement",
}


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


def _header_of(node, source):
    """The text of `node` up to its body, on one line."""
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(source[node.start_byte : end].decode().split())


def _name_of(node, source):
    if node.type == "expression_statement" and node.named_children:
        statement = node.named_children[0]
        target = statement.child_by_field_name("left")
        if statement.type == "assignment" and target is not None:
            return f"assignment {source[target.start_byte : target.end_byte].decode()}"
        return statement.type
    name = node.child_by_field_name("name")
    label = source[name.start_byte : name.end_byte].decode() if name else ""
    return f"{node.type.removesuffix('_definition')} {label}".strip()


def _kind_of(node):
    if (
        node.type == "expression_statement"
        and node.named_children
        and node.named_children[0].type == "assignment"
    ):
        return jevrev.state.ContentKind.ASSIGNMENT
    return jevrev.state.ContentKind.DEFINITION


def _contents(nodes, source, path, context=""):
    lines = source.decode().splitlines()

    def content(first, last, name, kind=jevrev.state.ContentKind.DEFINITION):
        return jevrev.state.FileContent(
            file=path,
            start=first + 1,
            end=last + 1,
            name=name,
            kind=kind,
            context=context,
            text="\n".join(lines[first : last + 1]),
        )

    for node in nodes:
        if node.type in _SKIPPED:
            continue
        above = _leading(node, _LEADING)
        start = (above[0] if above else node).start_point[0]
        definition = node.child_by_field_name("definition") or node
        name = _name_of(definition, source)
        body = definition.child_by_field_name("body")
        if definition.type == "class_definition" and body is not None:
            methods = [
                c
                for c in body.named_children
                if (c.child_by_field_name("definition") or c).type
                == "function_definition"
            ]
            if methods:
                first = _leading(methods[0], _LEADING) or [methods[0]]
                yield content(
                    first=start, last=first[0].start_point[0] - 1, name=name
                )
                yield from _contents(
                    nodes=methods,
                    source=source,
                    path=path,
                    context=_header_of(definition, source),
                )
                continue
        yield content(
            first=start,
            last=node.end_point[0],
            name=name,
            kind=_kind_of(definition),
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
    def contents(cls, source, path):
        return list(
            _contents(
                nodes=_PARSER.parse(source).root_node.named_children,
                source=source,
                path=path,
            )
        )

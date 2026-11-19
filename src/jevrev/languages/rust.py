"""Rust: each top-level item, each item of an inline module, and each method of an impl or trait;
runs of consts and statics are one range."""

import enum

import tree_sitter
import tree_sitter_rust

import jevrev.rules
from jevrev.languages import base

_PARSER = tree_sitter.Parser(tree_sitter.Language(tree_sitter_rust.language()))


class _Node(enum.StrEnum):
    """The types of tree-sitter-rust nodes the ranges depend on."""

    LINE_COMMENT = "line_comment"
    BLOCK_COMMENT = "block_comment"
    ATTRIBUTE_ITEM = "attribute_item"
    INNER_ATTRIBUTE_ITEM = "inner_attribute_item"
    USE_DECLARATION = "use_declaration"
    EXTERN_CRATE_DECLARATION = "extern_crate_declaration"
    EMPTY_STATEMENT = "empty_statement"
    CONST_ITEM = "const_item"
    STATIC_ITEM = "static_item"
    MOD_ITEM = "mod_item"
    IMPL_ITEM = "impl_item"
    TRAIT_ITEM = "trait_item"
    FUNCTION_ITEM = "function_item"
    FUNCTION_SIGNATURE_ITEM = "function_signature_item"


class _Field(enum.StrEnum):
    BODY = "body"


# Siblings that belong to the item after them.
_LEADING = frozenset(
    {_Node.LINE_COMMENT, _Node.BLOCK_COMMENT, _Node.ATTRIBUTE_ITEM}
)

# Items with nothing for the rule set to judge.
_SKIPPED = _LEADING | {
    _Node.USE_DECLARATION,
    _Node.EXTERN_CRATE_DECLARATION,
    _Node.INNER_ATTRIBUTE_ITEM,
    _Node.EMPTY_STATEMENT,
}

_MERGEABLE = frozenset({_Node.CONST_ITEM, _Node.STATIC_ITEM})

_METHODS = frozenset({_Node.FUNCTION_ITEM, _Node.FUNCTION_SIGNATURE_ITEM})


def _units(nodes):
    for node in nodes:
        if node.type in _SKIPPED:
            continue
        above = base.leading(node, _LEADING)
        body = node.child_by_field_name(_Field.BODY)
        if node.type == _Node.MOD_ITEM and body is not None:
            yield from _units(body.named_children)
            continue
        if (
            node.type in (_Node.IMPL_ITEM, _Node.TRAIT_ITEM)
            and body is not None
        ):
            methods = [c for c in body.named_children if c.type in _METHODS]
            if methods:
                yield from _units(methods)
                continue
        yield base.Unit(
            first=(above[0] if above else node).start_point[0],
            last=node.end_point[0],
            mergeable=node.type in _MERGEABLE,
        )


class Rust(base.Language):
    name = jevrev.rules.Language.RUST
    suffixes = (".rs",)
    test_marker = "#[cfg(test)]"

    @classmethod
    def states(cls, text):
        root = _PARSER.parse(text.encode(errors="surrogateescape")).root_node
        if root.has_error:
            raise base.ParseError("not valid Rust")
        return base.tile(_units(root.named_children), len(text.splitlines()))

"""Rust: each top-level item, each item of an inline module, and each method of an impl or trait."""

import tree_sitter
import tree_sitter_rust

import jevrev.rules
import jevrev.state
from jevrev.languages import base

_PARSER = tree_sitter.Parser(tree_sitter.Language(tree_sitter_rust.language()))

# Siblings that belong to the item after them.
_LEADING = {"line_comment", "block_comment", "attribute_item"}

# Items with nothing for the rule set to judge.
_SKIPPED = {
    "use_declaration",
    "extern_crate_declaration",
    "inner_attribute_item",
    "line_comment",
    "block_comment",
    "attribute_item",
    "empty_statement",
}

# The kind of each node type that is not a definition.
_NODE_KINDS = {
    "const_item": jevrev.state.ContentKind.CONST,
    "static_item": jevrev.state.ContentKind.STATIC,
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


def _is_test(node, source):
    return node.type == "attribute_item" and b"cfg(test)" in source[
        node.start_byte : node.end_byte
    ].replace(b" ", b"")


def _name_of(node, source):
    name = node.child_by_field_name("name")
    if name is None:
        name = node.child_by_field_name("type")
    label = source[name.start_byte : name.end_byte].decode() if name else ""
    return f"{node.type.removesuffix('_item')} {label}".strip()


def _contents(nodes, source, path, context=""):
    lines = source.decode().splitlines()
    for node in nodes:
        if node.type in _SKIPPED:
            continue
        above = _leading(node, _LEADING)
        if any(_is_test(a, source) for a in above):
            continue
        body = node.child_by_field_name("body")
        if node.type == "mod_item" and body is not None:
            yield from _contents(
                nodes=body.named_children,
                source=source,
                path=path,
                context=_header_of(node, source),
            )
            continue
        if node.type in ("impl_item", "trait_item") and body is not None:
            methods = [
                c
                for c in body.named_children
                if c.type in ("function_item", "function_signature_item")
            ]
            if methods:
                yield from _contents(
                    nodes=methods,
                    source=source,
                    path=path,
                    context=_header_of(node, source),
                )
                continue
        start = (above[0] if above else node).start_point[0]
        end = node.end_point[0]
        yield jevrev.state.FileContent(
            file=path,
            start=start + 1,
            end=end + 1,
            name=_name_of(node, source),
            kind=_NODE_KINDS.get(
                node.type, jevrev.state.ContentKind.DEFINITION
            ),
            context=context,
            text="\n".join(lines[start : end + 1]),
        )


class Rust(base.Language):
    name = jevrev.rules.Language.RUST
    suffixes = (".rs",)
    test_marker = "#[cfg(test)]"

    @classmethod
    def contents(cls, source, path):
        return list(
            _contents(
                nodes=_PARSER.parse(source).root_node.named_children,
                source=source,
                path=path,
            )
        )

use std::str::FromStr;

use tree_sitter::Node;

use super::tree::{self, named_children};
use super::{Merging, Syntax, Unit};

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::AsRefStr, strum::EnumString)]
#[strum(serialize_all = "snake_case")]
enum Kind {
    Comment,
    ImportStatement,
    ImportFromStatement,
    FutureImportStatement,
    PassStatement,
    ExpressionStatement,
    Assignment,
    ClassDefinition,
    FunctionDefinition,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::AsRefStr)]
#[strum(serialize_all = "snake_case")]
enum Field {
    Body,
    Definition,
}

const LEADING: &[Kind] = &[Kind::Comment];

const SKIPPED: &[Kind] = &[
    Kind::ImportStatement,
    Kind::ImportFromStatement,
    Kind::FutureImportStatement,
    Kind::PassStatement,
];

fn kind_of(node: Node<'_>) -> Option<Kind> {
    Kind::from_str(node.kind()).ok()
}

fn definition_of(node: Node<'_>) -> Node<'_> {
    node.child_by_field_name(Field::Definition.as_ref())
        .unwrap_or(node)
}

fn collect(nodes: &[Node<'_>], found: &mut Vec<Unit>) {
    for &node in nodes {
        if kind_of(node).is_some_and(|kind| LEADING.contains(&kind) || SKIPPED.contains(&kind)) {
            continue;
        }
        let definition = definition_of(node);
        let merging = match kind_of(definition) {
            Some(Kind::ClassDefinition) => {
                let methods: Vec<Node<'_>> = definition
                    .child_by_field_name(Field::Body.as_ref())
                    .map(named_children)
                    .unwrap_or_default()
                    .into_iter()
                    .filter(|child| {
                        kind_of(definition_of(*child)) == Some(Kind::FunctionDefinition)
                    })
                    .collect();
                if let Some(&first_method) = methods.first() {
                    let head = Unit::of(node, LEADING, Merging::Separate);
                    found.push(head.before(Unit::of(first_method, LEADING, Merging::Separate)));
                    collect(&methods, found);
                    continue;
                }
                Merging::Separate
            }
            Some(Kind::ExpressionStatement)
                if named_children(definition)
                    .first()
                    .is_some_and(|child| kind_of(*child) == Some(Kind::Assignment)) =>
            {
                Merging::Mergeable
            }
            _ => Merging::Separate,
        };
        found.push(Unit::of(node, LEADING, merging));
    }
}

pub(crate) fn units(source: &[u8]) -> Syntax<Vec<Unit>> {
    tree::parse(&tree_sitter_python::LANGUAGE.into(), source).map(|parsed| {
        let mut found = Vec::new();
        collect(&named_children(parsed.root_node()), &mut found);
        found
    })
}

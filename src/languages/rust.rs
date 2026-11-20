use std::str::FromStr;

use tree_sitter::Node;

use super::tree::{self, named_children};
use super::{Merging, Syntax, Unit};

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::AsRefStr, strum::EnumString)]
#[strum(serialize_all = "snake_case")]
enum Kind {
    LineComment,
    BlockComment,
    AttributeItem,
    InnerAttributeItem,
    UseDeclaration,
    ExternCrateDeclaration,
    EmptyStatement,
    ConstItem,
    StaticItem,
    ModItem,
    ImplItem,
    TraitItem,
    FunctionItem,
    FunctionSignatureItem,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::AsRefStr)]
#[strum(serialize_all = "snake_case")]
enum Field {
    Body,
}

const LEADING: &[Kind] = &[Kind::LineComment, Kind::BlockComment, Kind::AttributeItem];

const SKIPPED: &[Kind] = &[
    Kind::InnerAttributeItem,
    Kind::UseDeclaration,
    Kind::ExternCrateDeclaration,
    Kind::EmptyStatement,
];

fn kind_of(node: Node<'_>) -> Option<Kind> {
    Kind::from_str(node.kind()).ok()
}

fn collect(nodes: &[Node<'_>], found: &mut Vec<Unit>) {
    for &node in nodes {
        let body = node.child_by_field_name(Field::Body.as_ref());
        let merging = match (kind_of(node), body) {
            (Some(kind), _) if LEADING.contains(&kind) || SKIPPED.contains(&kind) => continue,
            (Some(Kind::ModItem), Some(body)) => {
                collect(&named_children(body), found);
                continue;
            }
            (Some(Kind::ImplItem | Kind::TraitItem), Some(body)) => {
                let methods: Vec<Node<'_>> = named_children(body)
                    .into_iter()
                    .filter(|c| {
                        matches!(
                            kind_of(*c),
                            Some(Kind::FunctionItem | Kind::FunctionSignatureItem)
                        )
                    })
                    .collect();
                if !methods.is_empty() {
                    collect(&methods, found);
                    continue;
                }
                Merging::Separate
            }
            (Some(Kind::ConstItem | Kind::StaticItem), _) => Merging::Mergeable,
            _ => Merging::Separate,
        };
        found.push(Unit::of(node, LEADING, merging));
    }
}

pub(crate) fn units(source: &[u8]) -> Syntax<Vec<Unit>> {
    tree::parse(&tree_sitter_rust::LANGUAGE.into(), source).map(|parsed| {
        let mut found = Vec::new();
        collect(&named_children(parsed.root_node()), &mut found);
        found
    })
}

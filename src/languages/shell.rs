use std::str::FromStr;

use tree_sitter::Node;

use super::tree::{self, named_children};
use super::{Merging, Syntax, Unit};

#[derive(Debug, Clone, Copy, PartialEq, Eq, strum::AsRefStr, strum::EnumString)]
#[strum(serialize_all = "snake_case")]
enum Kind {
    Comment,
    FunctionDefinition,
}

const LEADING: &[Kind] = &[Kind::Comment];

pub(crate) fn units(source: &[u8]) -> Syntax<Vec<Unit>> {
    tree::parse(&tree_sitter_bash::LANGUAGE.into(), source).map(|parsed| {
        let after_shebang = usize::from(source.starts_with(b"#!"));
        let unit = |first: Node<'_>, last: Node<'_>| {
            let leading = tree::leading(first, LEADING);
            Unit::spanning(
                leading.first().copied().unwrap_or(first),
                last,
                Merging::Separate,
            )
            .starting_at_or_after(after_shebang)
        };
        let mut found = Vec::new();
        let mut run: Vec<Node<'_>> = Vec::new();
        for node in named_children(parsed.root_node()) {
            match Kind::from_str(node.kind()).ok() {
                Some(Kind::Comment) => {}
                Some(Kind::FunctionDefinition) => {
                    if let (Some(first), Some(last)) = (run.first(), run.last()) {
                        found.push(unit(*first, *last));
                        run.clear();
                    }
                    found.push(unit(node, node));
                }
                None => run.push(node),
            }
        }
        if let (Some(first), Some(last)) = (run.first(), run.last()) {
            found.push(unit(*first, *last));
        }
        found
    })
}

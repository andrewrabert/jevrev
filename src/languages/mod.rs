pub mod python;
pub mod rust;
pub mod shell;

use std::collections::BTreeSet;
use std::sync::LazyLock;

use crate::lines::LineRange;
use crate::rules::Language;
use crate::source_path::SourcePath;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TestMarker {
    Line(&'static str),
    Absent,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Syntax<T> {
    Valid(T),
    Invalid,
}

impl<T> Syntax<T> {
    pub fn map<U>(self, f: impl FnOnce(T) -> U) -> Syntax<U> {
        match self {
            Syntax::Valid(value) => Syntax::Valid(f(value)),
            Syntax::Invalid => Syntax::Invalid,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Merging {
    Mergeable,
    Separate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Unit {
    first: usize,
    more: usize,
    merging: Merging,
}

impl Unit {
    pub fn of<K: AsRef<str>>(node: tree_sitter::Node<'_>, leading: &[K], merging: Merging) -> Unit {
        Unit::spanning(
            tree::leading(node, leading)
                .first()
                .copied()
                .unwrap_or(node),
            node,
            merging,
        )
    }

    pub fn spanning(
        first: tree_sitter::Node<'_>,
        last: tree_sitter::Node<'_>,
        merging: Merging,
    ) -> Unit {
        let first_row = first.start_position().row;
        Unit {
            first: first_row,
            more: last.end_position().row - first_row,
            merging,
        }
    }

    pub fn before(self, next: Unit) -> Unit {
        Unit {
            first: self.first,
            more: next.first - 1 - self.first,
            merging: self.merging,
        }
    }

    pub fn starting_at_or_after(self, after: usize) -> Unit {
        let last = self.first + self.more;
        let first = self.first.max(after).min(last);
        Unit {
            first,
            more: last - first,
            merging: self.merging,
        }
    }
}

pub(crate) fn tile(mut units: Vec<Unit>, lines: &[&[u8]]) -> Vec<LineRange> {
    units.sort_by_key(|unit| unit.first);
    let mut joined: Vec<Unit> = Vec::new();
    for unit in units {
        if let Some(previous) = joined.last_mut()
            && previous.merging == Merging::Mergeable
            && unit.merging == Merging::Mergeable
        {
            let previous_last = previous.first + previous.more;
            if unit.first <= previous_last + 2
                && lines[previous_last + 1..unit.first]
                    .iter()
                    .all(|line| line.trim_ascii().is_empty())
            {
                previous.more = unit.first + unit.more - previous.first;
                continue;
            }
        }
        joined.push(unit);
    }
    let mut ranges = Vec::new();
    let mut row = 0;
    for unit in joined {
        if unit.first > row {
            ranges.push(
                LineRange::rows(row, unit.first - 1).expect("the gap before a unit ends after row"),
            );
        }
        ranges.push(
            LineRange::rows(unit.first, unit.first + unit.more)
                .expect("a unit ends at or after its first row"),
        );
        row = unit.first + unit.more + 1;
    }
    if row < lines.len() {
        ranges.push(LineRange::rows(row, lines.len() - 1).expect("row is before the last line"));
    }
    ranges
}

pub(crate) fn lines(source: &[u8]) -> Vec<&[u8]> {
    let mut found: Vec<&[u8]> = source.split(|byte| *byte == b'\n').collect();
    if found.last().is_some_and(|line| line.is_empty()) {
        found.pop();
    }
    found
        .into_iter()
        .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
        .collect()
}

fn python_regex(pattern: &str) -> regex::Regex {
    let pattern = pattern
        .replace(r"\s", r"[\s\x1c-\x1f]")
        .replace(r"\S", r"[^\s\x1c-\x1f]")
        .replace('$', r"\n?\z");
    regex::Regex::new(&pattern).expect("a valid shebang pattern")
}

static SHEBANGS: LazyLock<Vec<(Language, Vec<regex::Regex>)>> = LazyLock::new(|| {
    vec![
        (
            Language::Python,
            vec![
                python_regex(r"^#!\S*/python[\d.]*(\s|$)"),
                python_regex(r"^#!\S*/env\s+(-\S+\s+)*python[\d.]*(\s|$)"),
                python_regex(r"^#!/usr/bin/env.*uv.*run"),
            ],
        ),
        (
            Language::Sh,
            vec![
                python_regex(r"^#!\S*/sh(\s|$)"),
                python_regex(r"^#!\S*/env\s+(-\S+\s+)*sh(\s|$)"),
            ],
        ),
    ]
});

pub fn detect(path: &SourcePath, source: &str) -> BTreeSet<Language> {
    let mut found = BTreeSet::new();
    if matches!(path.file_name(), "justfile" | "Justfile" | ".justfile") {
        found.insert(Language::Just);
    }
    match path.extension() {
        Some("rs") => {
            found.insert(Language::Rust);
        }
        Some("py") => {
            found.insert(Language::Python);
        }
        Some("just") => {
            found.insert(Language::Just);
        }
        Some("md") => {
            found.insert(Language::Markdown);
        }
        _ => {}
    }
    let first = source.lines().next().unwrap_or_default();
    found.extend(
        SHEBANGS
            .iter()
            .filter(|(_, patterns)| patterns.iter().any(|p| p.is_match(first)))
            .map(|(language, _)| *language),
    );
    found
}

pub fn ranges(language: Language, source: &[u8]) -> Syntax<Vec<LineRange>> {
    let lines = lines(source);
    let units = match language {
        Language::Rust => rust::units(source),
        Language::Python => python::units(source),
        Language::Sh => shell::units(source),
        Language::Just | Language::Markdown => Syntax::Valid(Vec::new()),
    };
    units.map(|units| tile(units, &lines))
}

pub fn test_marker(language: Language) -> TestMarker {
    match language {
        Language::Rust => TestMarker::Line("#[cfg(test)]"),
        Language::Python | Language::Sh | Language::Just | Language::Markdown => TestMarker::Absent,
    }
}

pub(crate) mod tree {
    use tree_sitter::{Language, Node, Parser, Tree};

    pub fn parse(grammar: &Language, source: &[u8]) -> super::Syntax<Tree> {
        let mut parser = Parser::new();
        parser
            .set_language(grammar)
            .expect("a grammar of this tree-sitter version");
        let tree = parser
            .parse(source, None)
            .expect("a parse without a timeout");
        if tree.root_node().has_error() {
            super::Syntax::Invalid
        } else {
            super::Syntax::Valid(tree)
        }
    }

    pub fn named_children<'tree>(node: Node<'tree>) -> Vec<Node<'tree>> {
        let mut cursor = node.walk();
        node.named_children(&mut cursor).collect()
    }

    pub fn row(node: Node<'_>) -> usize {
        node.start_position().row
    }

    pub fn end_row(node: Node<'_>) -> usize {
        node.end_position().row
    }

    pub fn leading<'tree, K: AsRef<str>>(node: Node<'tree>, kinds: &[K]) -> Vec<Node<'tree>> {
        let mut found: Vec<Node<'tree>> = Vec::new();
        let mut sibling = node.prev_sibling();
        while let Some(current) = sibling {
            if !kinds.iter().any(|kind| current.kind() == kind.as_ref()) {
                break;
            }
            let below = found.first().copied().unwrap_or(node);
            if row(below) as i64 - end_row(current) as i64 > 1 {
                break;
            }
            found.insert(0, current);
            sibling = current.prev_sibling();
        }
        found
    }
}

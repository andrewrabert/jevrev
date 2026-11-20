use std::path::PathBuf;

use crate::lines::LineRange;
use crate::non_empty::NonEmpty;
use crate::rules::Language;

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize, strum::AsRefStr,
)]
#[serde(rename_all = "lowercase")]
#[strum(serialize_all = "lowercase")]
pub enum StateKind {
    File,
    Hunk,
    Hunks,
    Language,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Extent {
    Lines,
    Whole,
}

impl StateKind {
    pub fn extent(self) -> Extent {
        match self {
            StateKind::Hunk | StateKind::Language => Extent::Lines,
            StateKind::File | StateKind::Hunks => Extent::Whole,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiffHunk {
    pub header: String,
    pub lines: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Body {
    File { text: String },
    Language { language: Language, text: String },
    Hunk(DiffHunk),
    Hunks(NonEmpty<DiffHunk>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct State {
    pub file: String,
    pub line_range: LineRange,
    pub sets: NonEmpty<PathBuf>,
    pub body: Body,
}

impl State {
    pub fn kind(&self) -> StateKind {
        match self.body {
            Body::File { .. } => StateKind::File,
            Body::Language { .. } => StateKind::Language,
            Body::Hunk(_) => StateKind::Hunk,
            Body::Hunks(_) => StateKind::Hunks,
        }
    }

    fn added(&self, text: &str) -> DiffHunk {
        let lines = text.lines().collect::<Vec<_>>();
        DiffHunk {
            header: format!("@@ -0,0 +{},{} @@", self.line_range.start(), lines.len()),
            lines: lines.iter().map(|line| format!("+{line}")).collect(),
        }
    }

    pub fn render(&self) -> String {
        let mut lines = vec![
            format!("diff --git a/{0} b/{0}", self.file),
            format!("--- a/{}", self.file),
            format!("+++ b/{}", self.file),
        ];
        match &self.body {
            Body::File { text } | Body::Language { text, .. } => {
                let hunk = self.added(text);
                lines.push(hunk.header);
                lines.extend(hunk.lines);
            }
            Body::Hunk(hunk) => lines.extend(hunk.lines.iter().cloned()),
            Body::Hunks(hunks) => {
                for hunk in hunks.iter() {
                    lines.push(hunk.header.clone());
                    lines.extend(hunk.lines.iter().cloned());
                }
            }
        }
        lines.join("\n")
    }
}

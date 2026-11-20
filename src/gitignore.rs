use ignore::Match;
use ignore::gitignore::{Gitignore, GitignoreBuilder};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pattern(String);

impl Pattern {
    pub fn new(text: &str) -> Pattern {
        Pattern(text.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Check {
    Included,
    Excluded,
    Unmatched,
}

#[derive(Debug, Clone)]
pub struct Spec(Gitignore);

impl Default for Spec {
    fn default() -> Spec {
        Spec(Gitignore::empty())
    }
}

impl Spec {
    pub fn new(patterns: Vec<Pattern>) -> Spec {
        Spec::from_lines(patterns.iter().map(Pattern::as_str))
    }

    pub fn from_lines<'line>(lines: impl IntoIterator<Item = &'line str>) -> Spec {
        let mut builder = GitignoreBuilder::new(".");
        builder.allow_unclosed_class(false);
        for line in lines {
            if let Err(e) = builder.add_line(None, line) {
                tracing::warn!("ignoring the pattern {line:?}: {e}");
            }
        }
        Spec(builder.build().unwrap_or_else(|e| {
            tracing::warn!("ignoring every pattern: {e}");
            Gitignore::empty()
        }))
    }

    pub fn from_bytes(contents: &[u8]) -> Spec {
        Spec::from_lines(String::from_utf8_lossy(contents).lines())
    }

    pub fn global() -> Spec {
        Spec(Gitignore::global().0)
    }

    pub fn check_entry(&self, path: &str, is_dir: bool) -> Check {
        match self.0.matched(path, is_dir) {
            Match::None => Check::Unmatched,
            Match::Ignore(_) => Check::Included,
            Match::Whitelist(_) => Check::Excluded,
        }
    }

    pub fn check(&self, path: &str) -> Check {
        let path = path
            .strip_prefix('/')
            .or_else(|| path.strip_prefix("./"))
            .unwrap_or(path);
        let (path, is_dir) = match path.strip_suffix('/') {
            Some(directory) => (directory, true),
            None => (path, false),
        };
        let above_included = path
            .match_indices('/')
            .any(|(at, _)| self.check_entry(&path[..at], true) == Check::Included);
        if above_included {
            Check::Included
        } else {
            self.check_entry(path, is_dir)
        }
    }
}

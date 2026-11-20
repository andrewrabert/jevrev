use anyhow::Context;
use ignore::Match;
use ignore::gitignore::{Gitignore, GitignoreBuilder};

use crate::Error;

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
    pub fn from_lines<'line>(lines: impl IntoIterator<Item = &'line str>) -> Result<Spec, Error> {
        let mut builder = GitignoreBuilder::new(".");
        builder.allow_unclosed_class(false);
        for line in lines {
            builder
                .add_line(None, line)
                .with_context(|| format!("the ignore pattern {line:?}"))?;
        }
        Ok(Spec(builder.build()?))
    }

    pub fn from_bytes(contents: &[u8]) -> Result<Spec, Error> {
        Spec::from_lines(std::str::from_utf8(contents)?.lines())
    }

    pub fn global() -> Result<Spec, Error> {
        match Gitignore::global() {
            (spec, None) => Ok(Spec(spec)),
            (_, Some(e)) => Err(Error::new(e).context("the global git excludes file")),
        }
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

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::Context;

use crate::Error;
use crate::ignore_rules::Ignore;
use crate::source_path::{SourcePath, SourcePathError};

pub fn walk(root: &Path, ignore: Option<&Ignore>) -> Result<Vec<SourcePath>, Error> {
    let top = std::fs::canonicalize(root).with_context(|| root.display().to_string())?;
    let mut seen: HashSet<PathBuf> = HashSet::new();
    let mut found = Vec::new();
    let inside = top.clone();
    let entries = ignore::WalkBuilder::new(root)
        .standard_filters(false)
        .follow_links(true)
        .sort_by_file_name(|a, b| a.cmp(b))
        .filter_entry(move |entry| {
            if entry.file_name() == ".git" {
                return false;
            }
            if !entry.path_is_symlink() {
                return true;
            }
            // A symlink that cannot be resolved is kept so the walk reports its error.
            std::fs::canonicalize(entry.path()).map_or(true, |real| !real.starts_with(&inside))
        })
        .build();
    for entry in entries {
        let entry = entry?;
        if !entry.file_type().is_some_and(|t| t.is_file()) {
            continue;
        }
        let real = std::fs::canonicalize(entry.path())
            .with_context(|| entry.path().display().to_string())?;
        if !seen.insert(real) {
            continue;
        }
        match SourcePath::from_base(root, entry.path(), ignore) {
            Ok(path) => found.push(path),
            Err(SourcePathError::Ignored) => {}
            Err(e) => return Err(Error::from(e).context(entry.path().display().to_string())),
        }
    }
    found.sort();
    Ok(found)
}

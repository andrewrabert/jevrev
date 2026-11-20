use std::collections::HashSet;
use std::path::{Path, PathBuf};

use crate::Error;

fn resolve(path: &Path) -> PathBuf {
    if let Ok(real) = std::fs::canonicalize(path) {
        return real;
    }
    match (path.parent(), path.file_name()) {
        (Some(parent), Some(name)) => resolve(parent).join(name),
        _ => path.to_path_buf(),
    }
}

pub fn walk(root: &Path) -> Result<Vec<String>, Error> {
    let top = resolve(root);
    let mut seen: HashSet<PathBuf> = HashSet::new();
    let mut found = Vec::new();
    let inside = top.clone();
    let entries = ignore::WalkBuilder::new(root)
        .standard_filters(false)
        .follow_links(true)
        .sort_by_file_name(|a, b| a.cmp(b))
        .filter_entry(move |entry| {
            entry.file_name() != ".git"
                && !(entry.path_is_symlink() && resolve(entry.path()).starts_with(&inside))
        })
        .build();
    for entry in entries.flatten() {
        if !entry.file_type().is_some_and(|t| t.is_file()) {
            continue;
        }
        if !seen.insert(resolve(entry.path())) {
            continue;
        }
        let relative = entry.path().strip_prefix(root).ok().and_then(Path::to_str);
        match relative {
            Some(relative) => found.push(relative.to_string()),
            None => tracing::info!(
                "skipping {}: not a UTF-8 path under {}",
                entry.path().display(),
                root.display()
            ),
        }
    }
    found.sort();
    Ok(found)
}

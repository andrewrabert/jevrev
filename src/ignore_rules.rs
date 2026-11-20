use std::collections::HashMap;

use crate::Error;
use crate::files::Files;
use crate::git::AboveSource;
use crate::gitignore::{Check, Spec};

const IGNORE_FILES: [&str; 2] = [".gitignore", ".ignore"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fate {
    Ignored,
    Kept,
}

fn parent_dirs(path: &str) -> Vec<String> {
    let parts: Vec<&str> = path.split('/').collect();
    let mut found = vec![String::new()];
    for n in 1..parts.len() {
        found.push(parts[..n].join("/"));
    }
    found
}

fn directory_of(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(directory, _)| directory)
}

#[derive(Debug, Clone)]
pub struct Ignore {
    prefix: String,
    excludes: Vec<Spec>,
    by_dir: HashMap<String, Vec<Spec>>,
}

impl Ignore {
    pub async fn load(
        files: &Files,
        excludes: Vec<Spec>,
        above: &AboveSource,
    ) -> Result<Ignore, Error> {
        let mut by_dir: HashMap<String, Vec<Spec>> = HashMap::new();
        for file in &above.ignore_files {
            by_dir
                .entry(file.directory.clone())
                .or_default()
                .push(Spec::from_bytes(&file.contents));
        }
        let mut found: Vec<String> = files
            .paths()?
            .into_iter()
            .filter(|path| IGNORE_FILES.contains(&path.rsplit('/').next().unwrap_or(path)))
            .collect();
        let rank = |path: &str| {
            let name = path.rsplit('/').next().unwrap_or(path);
            IGNORE_FILES.iter().position(|n| *n == name).unwrap_or(0)
        };
        found.sort_by(|a, b| (directory_of(a), rank(a)).cmp(&(directory_of(b), rank(b))));
        for path in found {
            let full = format!("{}{path}", above.prefix);
            by_dir
                .entry(directory_of(&full).to_string())
                .or_default()
                .push(Spec::from_bytes(&files.read(&path).await?));
        }
        Ok(Ignore {
            prefix: above.prefix.clone(),
            excludes,
            by_dir,
        })
    }

    fn ignored(&self, path: &str, is_dir: bool) -> bool {
        let mut result = Check::Unmatched;
        let mut apply = |check: Check| {
            if check != Check::Unmatched {
                result = check;
            }
        };
        for spec in &self.excludes {
            apply(spec.check_entry(path, is_dir));
        }
        for directory in parent_dirs(path) {
            let relative = if directory.is_empty() {
                path
            } else {
                path.get(directory.len() + 1..).unwrap_or("")
            };
            for spec in self.by_dir.get(&directory).into_iter().flatten() {
                apply(spec.check_entry(relative, is_dir));
            }
        }
        result == Check::Included
    }

    pub fn fate(&self, path: &str) -> Fate {
        let path = format!("{}{path}", self.prefix);
        let dirs = parent_dirs(&path);
        if dirs[1..].iter().any(|d| self.ignored(d, true)) || self.ignored(&path, false) {
            Fate::Ignored
        } else {
            Fate::Kept
        }
    }
}

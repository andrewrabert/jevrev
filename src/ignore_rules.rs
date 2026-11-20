use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::Context;

use crate::Error;
use crate::files::Files;
use crate::git::AboveSource;
use crate::gitignore::{Check, Spec};

const IGNORE_FILES: [&str; 2] = [".gitignore", ".ignore"];

#[derive(Debug, Clone)]
pub struct Ignore {
    subfolder: Option<PathBuf>,
    excludes: Vec<Spec>,
    by_dir: HashMap<PathBuf, Vec<Spec>>,
}

impl Ignore {
    pub async fn load(
        files: &Files,
        excludes: Vec<Spec>,
        above: &AboveSource,
    ) -> Result<Ignore, Error> {
        let mut by_dir: HashMap<PathBuf, Vec<Spec>> = HashMap::new();
        for file in &above.ignore_files {
            by_dir
                .entry(PathBuf::from(&file.directory))
                .or_default()
                .push(
                    Spec::from_bytes(&file.contents)
                        .with_context(|| format!("an ignore file in {:?}", file.directory))?,
                );
        }
        let in_repository = |path: &Path| match &above.subfolder {
            Some(subfolder) => subfolder.join(path),
            None => path.to_path_buf(),
        };
        let mut found: Vec<(PathBuf, usize, _)> = files
            .paths(None)?
            .into_iter()
            .filter_map(|path| {
                let rank = IGNORE_FILES.iter().position(|n| *n == path.file_name())?;
                let full = in_repository(Path::new(path.to_str()));
                let directory = full
                    .parent()
                    .expect("a file path has a parent")
                    .to_path_buf();
                Some((directory, rank, path))
            })
            .collect();
        found.sort();
        for (directory, _, path) in found {
            let text = files
                .read_text(&path)
                .await
                .map_err(|e| Error::from(e).context(path.to_string()))?;
            by_dir
                .entry(directory)
                .or_default()
                .push(Spec::from_lines(text.lines()).with_context(|| path.to_string())?);
        }
        Ok(Ignore {
            subfolder: above.subfolder.clone(),
            excludes,
            by_dir,
        })
    }

    fn ignored(&self, path: &Path, is_dir: bool) -> bool {
        let mut result = Check::Unmatched;
        let mut apply = |check: Check| {
            if check != Check::Unmatched {
                result = check;
            }
        };
        for spec in &self.excludes {
            apply(spec.check_entry(path, is_dir));
        }
        let mut directories: Vec<&Path> = path.ancestors().skip(1).collect();
        directories.reverse();
        for directory in directories {
            let relative = path
                .strip_prefix(directory)
                .expect("an ancestor of a path is its prefix");
            for spec in self.by_dir.get(directory).into_iter().flatten() {
                apply(spec.check_entry(relative, is_dir));
            }
        }
        result == Check::Included
    }

    pub fn ignores(&self, path: &Path) -> bool {
        let path = match &self.subfolder {
            Some(subfolder) => subfolder.join(path),
            None => path.to_path_buf(),
        };
        path.ancestors()
            .skip(1)
            .filter(|directory| !directory.as_os_str().is_empty())
            .any(|directory| self.ignored(directory, true))
            || self.ignored(&path, false)
    }
}

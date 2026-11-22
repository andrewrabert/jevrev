//! A scan's files on disk: a directory, or a single file.

use std::path::PathBuf;

use anyhow::anyhow;

use crate::Error;
use crate::files::{ReadTextError, Source};
use crate::source_path::{SourcePath, SourcePathError};

/// Whether a directory's walk leaves out what its ignore files leave out: `.ignore` files, and
/// inside a git repository its `.gitignore` files, `.git/info/exclude` and the global excludes
/// file, including those above the directory.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IgnoreFiles {
    Apply,
    Off,
}

#[derive(Debug, Clone)]
pub enum Disk {
    /// The regular files, and symlinks to regular files, under `root`, found by walking it as
    /// they are read, without following symlinks to directories, keeping hidden files, outside
    /// any `.git` directory, since no scan reads one.
    Directory { root: PathBuf, ignore: IgnoreFiles },
    /// The one file at `path`, named `name` in findings.
    File { path: PathBuf, name: SourcePath },
}

impl Source for Disk {
    fn paths(&self) -> Box<dyn Iterator<Item = Result<SourcePath, Error>> + '_> {
        match self {
            Disk::Directory { root, ignore } => Box::new(
                ignore::WalkBuilder::new(root)
                    .standard_filters(*ignore == IgnoreFiles::Apply)
                    .hidden(false)
                    .follow_links(false)
                    .sort_by_file_name(|a, b| a.cmp(b))
                    .build()
                    .filter_map(move |entry| {
                        let entry = match entry {
                            Ok(entry) => entry,
                            Err(e) => return Some(Err(Error::from(e))),
                        };
                        let file = entry.file_type().is_some_and(|kind| {
                            kind.is_file()
                                || (kind.is_symlink()
                                    && std::fs::metadata(entry.path())
                                        .is_ok_and(|target| target.is_file()))
                        });
                        if entry.depth() == 0 || !file {
                            return None;
                        }
                        match SourcePath::from_base(root, entry.path()) {
                            Ok(path) => Some(Ok(path)),
                            Err(SourcePathError::InsideGitDirectory) => None,
                            Err(e) => Some(Err(anyhow!("{}: {e}", entry.path().display()))),
                        }
                    }),
            ),
            Disk::File { name, .. } => Box::new(std::iter::once(Ok(name.clone()))),
        }
    }

    async fn read_text(&self, path: &SourcePath) -> Result<String, ReadTextError> {
        let file = match self {
            Disk::Directory { root, .. } => path.on_disk(root),
            Disk::File { path, .. } => path.clone(),
        };
        let contents = std::fs::read(file).map_err(Error::from)?;
        Ok(String::from_utf8(contents)?)
    }
}

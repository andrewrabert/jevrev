//! The `Source` trait every scan reads its files through.

use std::future::Future;
use std::string::FromUtf8Error;

use crate::Error;
use crate::gitignore::{Check, Spec};
use crate::source_path::SourcePath;

/// Where a scan's files come from: the disk, git, or GitHub.
pub trait Source {
    /// The files to scan, as each is found.
    fn paths(&self) -> Box<dyn Iterator<Item = Result<SourcePath, Error>> + '_>;

    /// Whether the source has, for each of `specs`, a path it matches, as the source itself has
    /// its paths. By default those are the files to scan.
    fn exists(&self, specs: &[Spec]) -> Result<bool, Error> {
        all_match(self.paths(), specs)
    }

    fn read_text(&self, path: &SourcePath) -> impl Future<Output = Result<String, ReadTextError>>;
}

/// Whether each of `specs` matches one of `paths`, reading only until every one has.
pub(crate) fn all_match(
    paths: impl Iterator<Item = Result<SourcePath, Error>>,
    specs: &[Spec],
) -> Result<bool, Error> {
    let mut unmatched: Vec<&Spec> = specs.iter().collect();
    if unmatched.is_empty() {
        return Ok(true);
    }
    for path in paths {
        let path = path?;
        unmatched.retain(|spec| spec.check(&path) != Check::Included);
        if unmatched.is_empty() {
            return Ok(true);
        }
    }
    Ok(false)
}

#[derive(Debug, thiserror::Error)]
pub enum ReadTextError {
    #[error("not UTF-8 text")]
    NotUtf8(#[from] FromUtf8Error),
    #[error(transparent)]
    Read(#[from] Error),
}

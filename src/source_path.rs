use std::ffi::OsString;
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct SourcePath(PathBuf);

#[derive(Debug, thiserror::Error)]
pub enum SourcePathError {
    #[error("empty path")]
    Empty,
    #[error("invalid path segment {0:?}")]
    Segment(String),
    #[error("not inside its base")]
    Outside,
    #[error("not UTF-8: {0:?}")]
    NotUtf8(OsString),
    #[error("inside a .git directory")]
    InsideGitDirectory,
}

impl SourcePath {
    pub fn new(path: &str) -> Result<SourcePath, SourcePathError> {
        let mut built = PathBuf::new();
        for segment in path.split('/') {
            if segment.is_empty() || segment == "." || segment == ".." {
                return Err(SourcePathError::Segment(segment.to_string()));
            }
            if segment == ".git" {
                return Err(SourcePathError::InsideGitDirectory);
            }
            built.push(segment);
        }
        Ok(SourcePath(built))
    }

    pub fn from_base(base: &Path, path: &Path) -> Result<SourcePath, SourcePathError> {
        let inside = path
            .strip_prefix(base)
            .map_err(|_| SourcePathError::Outside)?;
        let mut built = PathBuf::new();
        for component in inside.components() {
            let Component::Normal(name) = component else {
                return Err(SourcePathError::Segment(
                    component.as_os_str().to_string_lossy().into_owned(),
                ));
            };
            let name = name
                .to_os_string()
                .into_string()
                .map_err(SourcePathError::NotUtf8)?;
            if name == ".git" {
                return Err(SourcePathError::InsideGitDirectory);
            }
            built.push(name);
        }
        if built.as_os_str().is_empty() {
            return Err(SourcePathError::Empty);
        }
        Ok(SourcePath(built))
    }

    pub fn file_name(&self) -> &str {
        self.0
            .file_name()
            .and_then(|name| name.to_str())
            .expect("a source path ends in a UTF-8 name")
    }

    pub fn extension(&self) -> Option<&str> {
        self.0
            .extension()
            .map(|e| e.to_str().expect("a source path is UTF-8"))
    }

    pub fn to_str(&self) -> &str {
        self.0.to_str().expect("a source path is UTF-8")
    }

    pub fn on_disk(&self, root: &Path) -> PathBuf {
        root.join(&self.0)
    }
}

impl std::fmt::Display for SourcePath {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.to_str())
    }
}

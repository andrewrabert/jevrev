use anyhow::anyhow;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::string::FromUtf8Error;

use crate::Error;
use crate::git::CommitFiles;
use crate::github::api::Blobs;
use crate::ignore_rules::Ignore;
use crate::source_path::{SourcePath, SourcePathError};
use crate::walk::walk;

#[derive(Debug, Clone)]
pub struct OnDisk {
    root: PathBuf,
}

impl OnDisk {
    pub fn new(root: PathBuf) -> OnDisk {
        OnDisk { root }
    }
}

#[derive(Debug, Clone)]
pub struct OneFile {
    path: PathBuf,
    name: SourcePath,
}

impl OneFile {
    pub fn new(path: PathBuf, name: SourcePath) -> OneFile {
        OneFile { path, name }
    }
}

#[derive(Debug, Clone, Default)]
pub struct InMemory {
    contents: BTreeMap<SourcePath, Vec<u8>>,
}

impl InMemory {
    pub fn new(contents: BTreeMap<SourcePath, Vec<u8>>) -> InMemory {
        InMemory { contents }
    }
}

#[allow(clippy::large_enum_variant)]
pub enum Files {
    OnDisk(OnDisk),
    OneFile(OneFile),
    Commit(CommitFiles),
    InMemory(InMemory),
    Blobs(Blobs),
}

#[derive(Debug, thiserror::Error)]
pub enum ReadTextError {
    #[error("not UTF-8 text")]
    NotUtf8(#[from] FromUtf8Error),
    #[error(transparent)]
    Read(#[from] Error),
}

impl Files {
    pub fn paths(&self, ignore: Option<&Ignore>) -> Result<Vec<SourcePath>, Error> {
        let known: Vec<&SourcePath> = match self {
            Files::OnDisk(files) => return walk(&files.root, ignore),
            Files::Commit(files) => return files.paths(ignore),
            Files::Blobs(blobs) => return blobs.paths(ignore),
            Files::OneFile(file) => vec![&file.name],
            Files::InMemory(files) => files.contents.keys().collect(),
        };
        let mut found = Vec::new();
        for path in known {
            match SourcePath::new(path.to_str(), ignore) {
                Ok(path) => found.push(path),
                Err(SourcePathError::Ignored) => {}
                Err(e) => return Err(Error::from(e).context(path.to_string())),
            }
        }
        Ok(found)
    }

    pub async fn read_text(&self, path: &SourcePath) -> Result<String, ReadTextError> {
        match self {
            Files::OnDisk(files) => {
                let contents = std::fs::read(path.on_disk(&files.root)).map_err(Error::from)?;
                Ok(String::from_utf8(contents)?)
            }
            Files::OneFile(file) => {
                let contents = std::fs::read(&file.path).map_err(Error::from)?;
                Ok(String::from_utf8(contents)?)
            }
            Files::Commit(files) => files.read_text(path),
            Files::InMemory(files) => {
                let contents = files
                    .contents
                    .get(path)
                    .ok_or_else(|| anyhow!("no such file"))?;
                Ok(String::from_utf8(contents.clone())?)
            }
            Files::Blobs(blobs) => blobs.read_text(path).await,
        }
    }
}

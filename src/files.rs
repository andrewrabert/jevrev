use anyhow::anyhow;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::string::FromUtf8Error;

use crate::Error;
use crate::git::CommitFiles;
use crate::github::api::Blobs;
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
    name: String,
}

impl OneFile {
    pub fn new(path: PathBuf, name: String) -> OneFile {
        OneFile { path, name }
    }
}

#[derive(Debug, Clone, Default)]
pub struct InMemory {
    contents: BTreeMap<String, Vec<u8>>,
}

impl InMemory {
    pub fn new(contents: BTreeMap<String, Vec<u8>>) -> InMemory {
        InMemory {
            contents: contents
                .into_iter()
                .filter(|(path, _)| !path.split('/').any(|part| part == ".git"))
                .collect(),
        }
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
    pub fn paths(&self) -> Result<Vec<String>, Error> {
        match self {
            Files::OnDisk(files) => walk(&files.root),
            Files::OneFile(file) => Ok(vec![file.name.clone()]),
            Files::Commit(files) => files.paths(),
            Files::InMemory(files) => Ok(files.contents.keys().cloned().collect()),
            Files::Blobs(blobs) => Ok(blobs.paths()),
        }
    }

    pub async fn read_text(&self, path: &str) -> Result<String, ReadTextError> {
        match self {
            Files::OnDisk(files) => {
                let contents = std::fs::read(files.root.join(path)).map_err(Error::from)?;
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

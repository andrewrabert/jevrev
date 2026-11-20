use anyhow::{Context, anyhow};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

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

fn read_file(path: &Path, name: &str) -> Result<Vec<u8>, Error> {
    std::fs::read(path).with_context(|| name.to_string())
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

    pub async fn read(&self, path: &str) -> Result<Vec<u8>, Error> {
        match self {
            Files::OnDisk(files) => read_file(&files.root.join(path), path),
            Files::OneFile(file) => read_file(&file.path, path),
            Files::Commit(files) => files.read(path),
            Files::InMemory(files) => files
                .contents
                .get(path)
                .cloned()
                .ok_or_else(|| anyhow!("{path}: no such file")),
            Files::Blobs(blobs) => blobs.read(path).await,
        }
    }
}

use std::collections::BTreeSet;

use gix::objs::FindExt as _;
use gix::odb::pack;

use super::Pack;

#[derive(Debug, thiserror::Error)]
pub enum PackError {
    #[error("{id} is not a commit or a tree")]
    NotCommitOrTree { id: gix::hash::ObjectId },
    #[error("finding object {id}")]
    Find {
        id: gix::hash::ObjectId,
        #[source]
        source: gix::Error,
    },
    #[error("object {id} is missing")]
    Missing { id: gix::hash::ObjectId },
    #[error("listing the tree {id}")]
    Traverse {
        id: gix::hash::ObjectId,
        #[source]
        source: gix::Error,
    },
    #[error("writing the pack")]
    Write(#[source] gix::Error),
}

pub fn pack(
    objects: &impl gix::objs::Find,
    tips: &[gix::hash::ObjectId],
) -> Result<Pack, PackError> {
    let mut ids = BTreeSet::new();
    let mut buffer = Vec::new();
    for &tip in tips {
        let data = objects
            .try_find(&tip, &mut buffer)
            .map_err(|source| PackError::Find { id: tip, source })?
            .ok_or(PackError::Missing { id: tip })?;
        let root = match data.kind {
            gix::objs::Kind::Commit => {
                let tree = data
                    .decode()
                    .map_err(|source| PackError::Find { id: tip, source })?
                    .into_commit()
                    .ok_or(PackError::NotCommitOrTree { id: tip })?
                    .tree();
                ids.insert(tip);
                tree
            }
            gix::objs::Kind::Tree => tip,
            _ => return Err(PackError::NotCommitOrTree { id: tip }),
        };
        ids.insert(root);
        let mut tree_buffer = Vec::new();
        let tree = objects
            .find_tree_iter(&root, &mut tree_buffer)
            .map_err(|source| PackError::Find { id: root, source })?;
        let mut recorder = gix::traverse::tree::Recorder::default();
        gix::traverse::tree::breadthfirst(
            tree,
            gix::traverse::tree::breadthfirst::State::default(),
            objects,
            &mut recorder,
        )
        .map_err(|source| PackError::Traverse { id: root, source })?;
        ids.extend(
            recorder
                .records
                .into_iter()
                .filter(|entry| !entry.mode.is_commit())
                .map(|entry| entry.oid),
        );
    }

    let mut entries = Vec::with_capacity(ids.len());
    for id in ids {
        let data = objects
            .try_find(&id, &mut buffer)
            .map_err(|source| PackError::Find { id, source })?
            .ok_or(PackError::Missing { id })?;
        let count = pack::data::output::Count::from_data(id, None);
        entries.push(
            pack::data::output::Entry::from_data(&count, &data, gix::zlib::Compression::DEFAULT)
                .map_err(PackError::Write)?,
        );
    }
    let mut bytes = Vec::new();
    let num_entries = u32::try_from(entries.len()).expect("a pack holds fewer than 2^32 objects");
    let mut writer = pack::data::output::bytes::FromEntriesIter::new(
        std::iter::once(Ok(entries)),
        &mut bytes,
        num_entries,
        pack::data::Version::V2,
        gix::hash::Kind::Sha1,
    );
    for written in &mut writer {
        written.map_err(PackError::Write)?;
    }
    drop(writer);
    Ok(Pack::from(bytes))
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use gix::hash::ObjectId;
    use gix::objs::{Find as _, FindExt as _};

    use super::{PackError, pack};
    use crate::memgit::Objects;

    fn commit(repo: &gix::Repository, files: &[(&str, &str)], parents: &[ObjectId]) -> ObjectId {
        let mut editor = repo.edit_tree(repo.empty_tree().id).unwrap();
        for (path, text) in files {
            let blob = repo.write_blob(text).unwrap();
            editor
                .upsert(*path, gix::object::tree::EntryKind::Blob, blob)
                .unwrap();
        }
        let tree = editor.write().unwrap();
        let signature = gix::actor::SignatureRef {
            name: "t".into(),
            email: "t@t".into(),
            time: "0 +0000",
        };
        repo.commit_as(
            signature,
            signature,
            "HEAD",
            "commit",
            tree,
            parents.iter().copied(),
        )
        .unwrap()
        .detach()
    }

    fn repo(dir: &Path) -> [ObjectId; 3] {
        let repo = gix::init(dir).unwrap();
        let mut parents = Vec::new();
        for text in ["zero\n", "one\n", "two\n"] {
            let id = commit(&repo, &[("a.txt", text), ("sub/b.txt", "kept\n")], &parents);
            parents = vec![id];
        }
        let head = parents[0];
        let one = repo
            .find_commit(head)
            .unwrap()
            .parent_ids()
            .next()
            .unwrap()
            .detach();
        let zero = repo
            .find_commit(one)
            .unwrap()
            .parent_ids()
            .next()
            .unwrap()
            .detach();
        [zero, one, head]
    }

    fn blob_in_tree(objects: &Objects, tree: &ObjectId, path: &str) -> Option<Vec<u8>> {
        let mut buffer = Vec::new();
        let mut lookup = Vec::new();
        let entry = objects
            .find_tree_iter(tree, &mut buffer)
            .unwrap()
            .lookup_entry_by_path(objects, &mut lookup, path)
            .unwrap()?;
        Some(
            objects
                .find_blob(&entry.oid, &mut lookup)
                .unwrap()
                .data
                .to_vec(),
        )
    }

    fn tree_of(objects: &Objects, commit: &ObjectId) -> ObjectId {
        objects.find_commit(commit, &mut Vec::new()).unwrap().tree()
    }

    #[test]
    fn commits_of_a_local_repo_are_packed_without_their_history() {
        let dir = tempfile::tempdir().unwrap();
        let [first, base, head] = repo(dir.path());
        let local = gix::open(dir.path()).unwrap();
        let objects = Objects::new(pack(&local, &[base, head]).unwrap()).unwrap();
        drop(local);
        drop(dir);

        assert_eq!(
            blob_in_tree(&objects, &tree_of(&objects, &base), "a.txt").unwrap(),
            b"one\n"
        );
        let head_tree = tree_of(&objects, &head);
        assert_eq!(
            blob_in_tree(&objects, &head_tree, "a.txt").unwrap(),
            b"two\n"
        );
        assert_eq!(
            blob_in_tree(&objects, &head_tree, "sub/b.txt").unwrap(),
            b"kept\n"
        );
        assert!(objects.try_find(&first, &mut Vec::new()).unwrap().is_none());
    }

    fn object_files(dir: &Path) -> Vec<std::path::PathBuf> {
        fn walk(dir: &Path, found: &mut Vec<std::path::PathBuf>) {
            for entry in std::fs::read_dir(dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    walk(&path, found);
                } else {
                    found.push(path);
                }
            }
        }
        let mut found = Vec::new();
        walk(&dir.join(".git/objects"), &mut found);
        found.sort();
        found
    }

    #[test]
    fn a_tree_written_only_to_memory_is_packed() {
        let dir = tempfile::tempdir().unwrap();
        let [_, _, head] = repo(dir.path());
        let before = object_files(dir.path());

        let local = gix::open(dir.path()).unwrap().with_object_memory();
        let head_tree = local.find_commit(head).unwrap().tree_id().unwrap();
        let blob = local.write_blob("dirty\n").unwrap().detach();
        let mut editor = local.edit_tree(head_tree).unwrap();
        editor
            .upsert("a.txt", gix::object::tree::EntryKind::Blob, blob)
            .unwrap()
            .upsert("new.txt", gix::object::tree::EntryKind::Blob, blob)
            .unwrap();
        let root = editor.write().unwrap().detach();
        let objects = Objects::new(pack(&local, &[root]).unwrap()).unwrap();

        assert_eq!(blob_in_tree(&objects, &root, "a.txt").unwrap(), b"dirty\n");
        assert_eq!(
            blob_in_tree(&objects, &root, "new.txt").unwrap(),
            b"dirty\n"
        );
        assert_eq!(
            blob_in_tree(&objects, &root, "sub/b.txt").unwrap(),
            b"kept\n"
        );
        assert_eq!(object_files(dir.path()), before);
    }

    #[test]
    fn a_blob_is_not_a_tip() {
        let dir = tempfile::tempdir().unwrap();
        let [_, _, head] = repo(dir.path());
        let local = gix::open(dir.path()).unwrap();
        let blob = local
            .find_commit(head)
            .unwrap()
            .tree()
            .unwrap()
            .lookup_entry_by_path("a.txt")
            .unwrap()
            .unwrap()
            .object_id();
        assert!(matches!(
            pack(&local, &[blob]),
            Err(PackError::NotCommitOrTree { .. })
        ));
    }
}

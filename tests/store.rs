use std::path::{Path, PathBuf};

use jevrev::store::{Stored, open};

fn fixture_reviews() -> Vec<PathBuf> {
    let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/review");
    let mut found: Vec<PathBuf> = std::fs::read_dir(directory)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|e| e == "json"))
        .collect();
    found.sort();
    found
}

#[test]
fn reviews_load_and_round_trip() {
    let directory = tempfile::tempdir().unwrap();
    let files = fixture_reviews();
    assert_eq!(files.len(), 8);
    for file in files {
        let Stored::Review(review) = open(&file, |store| store.load()).unwrap() else {
            panic!("{} holds no review", file.display());
        };
        let saved = directory.path().join(file.file_name().unwrap());
        open(&saved, |store| store.save(&review)).unwrap();
        assert_eq!(
            std::fs::read(&saved).unwrap(),
            std::fs::read(&file).unwrap()
        );
        let Stored::Review(again) = open(&saved, |store| store.load()).unwrap() else {
            panic!("{} lost its review", saved.display());
        };
        assert_eq!(again, review, "{}", file.display());
    }
    let empty = directory.path().join("empty.json");
    assert_eq!(open(&empty, |store| store.load()).unwrap(), Stored::Empty);
}

#[test]
fn a_locked_review_file_fails_at_once() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("locked.json");
    let error = open(&path, |_| open(&path, |store| store.load())).unwrap_err();
    assert_eq!(error.to_string(), "the review file is locked");
}

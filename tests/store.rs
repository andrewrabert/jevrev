use std::path::{Path, PathBuf};

use jevrev::output::review_json;
use jevrev::store::{JudgeError, Judgment, Review, Stored, Verdict, open};

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

fn fixture(path: &Path) -> Review {
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}

#[test]
fn reviews_save_and_load() {
    let directory = tempfile::tempdir().unwrap();
    let files = fixture_reviews();
    assert_eq!(files.len(), 8);
    for file in files {
        let review = fixture(&file);
        assert_eq!(
            review_json(&review),
            std::fs::read_to_string(&file).unwrap(),
            "{}",
            file.display()
        );
        let saved = directory
            .path()
            .join(file.file_name().unwrap())
            .with_extension("redb");
        open(&saved, |store| store.save(&review)).unwrap();
        let Stored::Review(again) = open(&saved, |store| store.load()).unwrap() else {
            panic!("{} lost its review", saved.display());
        };
        assert_eq!(again, review, "{}", file.display());
    }
    let empty = directory.path().join("empty.redb");
    assert_eq!(open(&empty, |store| store.load()).unwrap(), Stored::Empty);
}

#[test]
fn a_judgment_adds_only_its_verdict() {
    let directory = tempfile::tempdir().unwrap();
    let mut review =
        fixture(&Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/review/pending.json"));
    let path = directory.path().join("pending.redb");
    open(&path, |store| store.save(&review)).unwrap();
    let judgment = Judgment {
        verdict: Verdict::Rejected,
        reason: "Not a constant.".to_string(),
    };
    open(&path, |store| Ok(store.judge(2, &judgment)))
        .unwrap()
        .unwrap();
    review.findings[1].judgment = Some(judgment);
    let Stored::Review(again) = open(&path, |store| store.load()).unwrap() else {
        panic!("the review is lost");
    };
    assert_eq!(again, review);
}

#[test]
fn a_judgment_needs_a_review_an_existing_finding_and_no_verdict() {
    let directory = tempfile::tempdir().unwrap();
    let judgment = Judgment {
        verdict: Verdict::Confirmed,
        reason: "r".to_string(),
    };
    let judge = |path: &Path, index| open(path, |store| Ok(store.judge(index, &judgment))).unwrap();
    let empty = directory.path().join("empty.redb");
    assert!(matches!(judge(&empty, 1), Err(JudgeError::NoReview)));
    assert_eq!(open(&empty, |store| store.load()).unwrap(), Stored::Empty);
    let review =
        fixture(&Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/review/pending.json"));
    let path = directory.path().join("pending.redb");
    open(&path, |store| store.save(&review)).unwrap();
    assert_eq!(
        judge(&path, 5).unwrap_err().to_string(),
        "index: 5 is not in 1..4"
    );
    assert_eq!(
        judge(&path, 3).unwrap_err().to_string(),
        "index: finding 3 is already rejected"
    );
    let Stored::Review(again) = open(&path, |store| store.load()).unwrap() else {
        panic!("the review is lost");
    };
    assert_eq!(again, review);
}

#[test]
fn a_locked_review_file_fails_at_once() {
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("locked.redb");
    let error = open(&path, |_| open(&path, |store| store.load())).unwrap_err();
    assert_eq!(error.to_string(), "the review file is locked");
}

#[test]
fn a_json_review_file_is_not_a_review_file() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/review/pending.json");
    let copy = tempfile::tempdir().unwrap();
    let copy = copy.path().join("pending.json");
    std::fs::copy(path, &copy).unwrap();
    assert!(open(&copy, |store| store.load()).is_err());
    assert_eq!(
        std::fs::read(&copy).unwrap(),
        std::fs::read(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/review/pending.json")
        )
        .unwrap()
    );
}

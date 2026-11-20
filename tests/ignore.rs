use std::collections::BTreeMap;

use jevrev::files::{Files, InMemory};
use jevrev::git::{AboveSource, IgnoreFile};
use jevrev::gitignore::Spec;
use jevrev::ignore_rules::{Fate, Ignore};

fn files(entries: &[(&str, &str)]) -> Files {
    Files::InMemory(InMemory::new(
        entries
            .iter()
            .map(|(path, text)| (path.to_string(), text.as_bytes().to_vec()))
            .collect::<BTreeMap<_, _>>(),
    ))
}

#[tokio::test]
async fn ignore_files_apply_with_dot_ignore_winning() {
    let source = files(&[
        (".gitignore", "*.log\nbuild/\n!keep.log\n"),
        (".ignore", "keep.log\n"),
        ("sub/.gitignore", "!*.log\nlocal.txt\n"),
        ("a.log", ""),
        ("keep.log", ""),
        ("sub/b.log", ""),
        ("sub/local.txt", ""),
        ("build/keep.log", ""),
        ("build/x.txt", ""),
        ("secret.txt", ""),
        ("x.py", ""),
        (".git/config", ""),
    ]);
    let ignore = Ignore::load(
        &source,
        vec![Spec::from_bytes(b"secret.txt\n").unwrap()],
        &AboveSource::default(),
    )
    .await
    .unwrap();
    let fate = |path: &str| ignore.fate(path);
    assert_eq!(fate("a.log"), Fate::Ignored);
    assert_eq!(fate("keep.log"), Fate::Ignored);
    assert_eq!(fate("sub/b.log"), Fate::Kept);
    assert_eq!(fate("sub/local.txt"), Fate::Ignored);
    assert_eq!(fate("build/keep.log"), Fate::Ignored);
    assert_eq!(fate("build/x.txt"), Fate::Ignored);
    assert_eq!(fate("secret.txt"), Fate::Ignored);
    assert_eq!(fate("x.py"), Fate::Kept);
    assert!(
        !source
            .paths()
            .unwrap()
            .iter()
            .any(|p| p.starts_with(".git/"))
    );

    let below = files(&[("a.txt", ""), ("b.md", ""), (".gitignore", "!c.txt\n")]);
    let above = AboveSource {
        prefix: "pkg/".into(),
        ignore_files: vec![
            IgnoreFile {
                directory: String::new(),
                contents: b"pkg/a.txt\nc.txt\n".to_vec(),
            },
            IgnoreFile {
                directory: String::new(),
                contents: b"*.md\n".to_vec(),
            },
        ],
    };
    let ignore = Ignore::load(&below, Vec::new(), &above).await.unwrap();
    assert_eq!(ignore.fate("a.txt"), Fate::Ignored);
    assert_eq!(ignore.fate("b.md"), Fate::Ignored);
    assert_eq!(ignore.fate("c.txt"), Fate::Kept);
    assert_eq!(ignore.fate(".gitignore"), Fate::Kept);
}

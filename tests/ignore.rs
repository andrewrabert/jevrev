use std::collections::BTreeMap;

use jevrev::files::{Files, InMemory};
use jevrev::git::{AboveSource, IgnoreFile};
use jevrev::gitignore::Spec;
use std::path::PathBuf;

use jevrev::ignore_rules::Ignore;
use jevrev::source_path::{SourcePath, SourcePathError};

fn files(entries: &[(&str, &str)]) -> Files {
    Files::InMemory(InMemory::new(
        entries
            .iter()
            .map(|(path, text)| {
                (
                    SourcePath::new(path, None).unwrap(),
                    text.as_bytes().to_vec(),
                )
            })
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
    ]);
    let ignore = Ignore::load(
        &source,
        vec![Spec::from_bytes(b"secret.txt\n").unwrap()],
        &AboveSource::default(),
    )
    .await
    .unwrap();
    let kept = |ignore: &Ignore, path: &str| match SourcePath::new(path, Some(ignore)) {
        Ok(_) => true,
        Err(SourcePathError::Ignored) => false,
        Err(e) => panic!("{path}: {e}"),
    };
    assert!(!kept(&ignore, "a.log"));
    assert!(!kept(&ignore, "keep.log"));
    assert!(kept(&ignore, "sub/b.log"));
    assert!(!kept(&ignore, "sub/local.txt"));
    assert!(!kept(&ignore, "build/keep.log"));
    assert!(!kept(&ignore, "build/x.txt"));
    assert!(!kept(&ignore, "secret.txt"));
    assert!(kept(&ignore, "x.py"));
    assert!(matches!(
        SourcePath::new(".git/config", None),
        Err(SourcePathError::InsideGitDirectory)
    ));

    let below = files(&[("a.txt", ""), ("b.md", ""), (".gitignore", "!c.txt\n")]);
    let above = AboveSource {
        subfolder: Some(PathBuf::from("pkg")),
        ignore_files: vec![
            IgnoreFile {
                directory: PathBuf::new(),
                contents: b"pkg/a.txt\nc.txt\n".to_vec(),
            },
            IgnoreFile {
                directory: PathBuf::new(),
                contents: b"*.md\n".to_vec(),
            },
        ],
    };
    let ignore = Ignore::load(&below, Vec::new(), &above).await.unwrap();
    assert!(!kept(&ignore, "a.txt"));
    assert!(!kept(&ignore, "b.md"));
    assert!(kept(&ignore, "c.txt"));
    assert!(kept(&ignore, ".gitignore"));
}

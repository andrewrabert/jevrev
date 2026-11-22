use std::os::unix::fs::symlink;
use std::path::Path;

use jevrev::disk::{Disk, IgnoreFiles};
use jevrev::files::Source;

fn write(root: &Path, path: &str, text: &str) {
    let path = root.join(path);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

fn tree(root: &Path) {
    write(root, ".gitignore", "build/\n");
    write(root, ".ignore", "scratch\n");
    write(root, "build/out.txt", "");
    write(root, "scratch", "");
    write(root, ".hidden", "");
    write(root, "src/a.py", "");
    symlink("src/a.py", root.join("link.py")).unwrap();
}

fn paths(root: &Path, ignore: IgnoreFiles) -> Vec<String> {
    let disk = Disk::Directory {
        root: root.to_path_buf(),
        ignore,
    };
    let mut found: Vec<String> = disk.paths().map(|path| path.unwrap().to_string()).collect();
    found.sort();
    found
}

#[test]
fn in_a_git_repository_its_ignore_files_apply() {
    let dir = tempfile::tempdir().unwrap();
    tree(dir.path());
    write(dir.path(), ".git/HEAD", "");

    assert_eq!(
        paths(dir.path(), IgnoreFiles::Apply),
        [".gitignore", ".hidden", ".ignore", "link.py", "src/a.py"]
    );
}

#[test]
fn outside_a_git_repository_only_dot_ignore_applies() {
    let dir = tempfile::tempdir().unwrap();
    tree(dir.path());

    assert_eq!(
        paths(dir.path(), IgnoreFiles::Apply),
        [
            ".gitignore",
            ".hidden",
            ".ignore",
            "build/out.txt",
            "link.py",
            "src/a.py"
        ]
    );
}

#[test]
fn without_ignore_every_regular_file_outside_dot_git_is_listed() {
    let dir = tempfile::tempdir().unwrap();
    tree(dir.path());
    write(dir.path(), ".git/HEAD", "");

    assert_eq!(
        paths(dir.path(), IgnoreFiles::Off),
        [
            ".gitignore",
            ".hidden",
            ".ignore",
            "build/out.txt",
            "link.py",
            "scratch",
            "src/a.py"
        ]
    );
}

#[test]
fn exists_sees_the_walked_paths() {
    let dir = tempfile::tempdir().unwrap();
    tree(dir.path());
    write(dir.path(), ".git/HEAD", "");
    let files = Disk::Directory {
        root: dir.path().to_path_buf(),
        ignore: IgnoreFiles::Apply,
    };
    let exists = |pattern: &str| {
        files
            .exists(&[jevrev::gitignore::Spec::from_lines([pattern]).unwrap()])
            .unwrap()
    };

    assert!(!exists("/build/"));
    assert!(!exists("scratch"));
    assert!(exists("*.py"));
    assert!(exists("/src/"));
    assert!(!exists("/missing"));
    let both = |patterns: [&str; 2]| {
        files
            .exists(
                &patterns.map(|pattern| jevrev::gitignore::Spec::from_lines([pattern]).unwrap()),
            )
            .unwrap()
    };
    assert!(both(["/src/", "link.py"]));
    assert!(!both(["/src/", "scratch"]));
}

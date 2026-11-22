use std::path::Path;
use std::process::Command;

use jevrev::git::GitRepo;

fn git(dir: &Path, args: &[&str]) {
    let status = Command::new("git")
        .args(args)
        .current_dir(dir)
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .status()
        .unwrap();
    assert!(status.success(), "git {args:?}");
}

fn write(root: &Path, path: &str, text: &str) {
    let path = root.join(path);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

fn names(paths: &[jevrev::source_path::SourcePath]) -> Vec<String> {
    paths.iter().map(|path| path.to_string()).collect()
}

#[test]
fn uncommitted_files_follow_only_git_rules() {
    let dir = tempfile::tempdir().unwrap();
    let root = std::fs::canonicalize(dir.path()).unwrap();
    git(&root, &["init", "-q"]);
    write(&root, ".gitignore", "build/\n*.log\n");
    write(&root, ".ignore", "new.py\n");
    write(&root, "tracked.py", "");
    write(&root, "forced.log", "");
    git(&root, &["add", ".gitignore", ".ignore", "tracked.py"]);
    git(&root, &["add", "-f", "forced.log"]);
    write(&root, "new.py", "");
    write(&root, "excluded.py", "");
    write(&root, ".git/info/exclude", "excluded.py\n");
    write(&root, "build/out.py", "");
    write(&root, "debug.log", "");
    write(&root, "sub/inner.py", "");
    write(&root, "configured.py", "");
    let excludes = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(excludes.path(), "configured.py\n").unwrap();
    git(
        &root,
        &[
            "config",
            "core.excludesFile",
            excludes.path().to_str().unwrap(),
        ],
    );

    let tree = GitRepo::open(&root).unwrap().working_tree().unwrap();

    assert_eq!(
        names(&tree.tracked),
        [".gitignore", ".ignore", "forced.log", "tracked.py"]
    );
    assert_eq!(names(&tree.untracked), ["new.py", "sub/inner.py"]);

    let tree = GitRepo::open(&root.join("sub"))
        .unwrap()
        .working_tree()
        .unwrap();

    assert!(tree.tracked.is_empty());
    assert_eq!(names(&tree.untracked), ["inner.py"]);
}

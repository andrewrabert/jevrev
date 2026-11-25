use std::io::Write as _;
use std::path::Path;

use jevrev::git::GitRepo;

fn stage(repo: &gix::Repository, root: &Path, paths: &[&str]) {
    let mut editor = repo.edit_tree(repo.empty_tree().id).unwrap();
    for path in paths {
        let blob = repo
            .write_blob(std::fs::read(root.join(path)).unwrap())
            .unwrap();
        editor
            .upsert(*path, gix::object::tree::EntryKind::Blob, blob)
            .unwrap();
    }
    let tree = editor.write().unwrap();
    repo.index_from_tree(&tree)
        .unwrap()
        .write(Default::default())
        .unwrap();
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
    let repo = gix::init(&root).unwrap();
    write(&root, ".gitignore", "build/\n*.log\n");
    write(&root, ".ignore", "new.py\n");
    write(&root, "tracked.py", "");
    write(&root, "forced.log", "");
    stage(
        &repo,
        &root,
        &[".gitignore", ".ignore", "tracked.py", "forced.log"],
    );
    write(&root, "new.py", "");
    write(&root, "excluded.py", "");
    write(&root, ".git/info/exclude", "excluded.py\n");
    write(&root, "build/out.py", "");
    write(&root, "debug.log", "");
    write(&root, "sub/inner.py", "");
    write(&root, "configured.py", "");
    let excludes = tempfile::NamedTempFile::new().unwrap();
    std::fs::write(excludes.path(), "configured.py\n").unwrap();
    writeln!(
        std::fs::OpenOptions::new()
            .append(true)
            .open(root.join(".git/config"))
            .unwrap(),
        "[core]\n\texcludesFile = {}",
        excludes.path().display()
    )
    .unwrap();

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

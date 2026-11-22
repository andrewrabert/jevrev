use crate::files::{ReadTextError, Source};
use crate::scan::Changes;
use crate::source_path::{SourcePath, SourcePathError};
use anyhow::{Context, anyhow, bail};
use std::collections::BTreeMap;
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

use gix::diff::blob::unified_diff::{ConsumeHunk, DiffLineKind, HunkHeader};

use crate::Error;
use crate::lines::LineRange;
use crate::non_empty::NonEmpty;

#[nutype::nutype(
    validate(regex = "^[0-9a-f]{40}$"),
    derive(
        Debug,
        Clone,
        PartialEq,
        Eq,
        PartialOrd,
        Ord,
        Hash,
        Display,
        Deref,
        Serialize,
        Deserialize
    )
)]
pub struct GitObjectId(String);

impl GitObjectId {
    pub fn parse(text: &str) -> Result<GitObjectId, Error> {
        GitObjectId::try_new(text.to_string()).map_err(|_| anyhow!("{text:?} is not an object id"))
    }
}

impl From<gix::hash::ObjectId> for GitObjectId {
    fn from(id: gix::hash::ObjectId) -> GitObjectId {
        GitObjectId::try_new(id.to_string()).expect("a SHA-1 object id is 40 lowercase hex digits")
    }
}

impl From<&GitObjectId> for gix::hash::ObjectId {
    fn from(id: &GitObjectId) -> gix::hash::ObjectId {
        gix::hash::ObjectId::from_hex(id.as_bytes()).expect("a GitObjectId is 40 hex digits")
    }
}

#[nutype::nutype(
    validate(regex = r"^[^\s\x00-\x1f\x7f-][^\s\x00-\x1f\x7f]*$"),
    derive(Debug, Clone, PartialEq, Eq, Display, Deref)
)]
pub struct GitRevision(String);

impl GitRevision {
    pub fn parse(text: &str) -> Result<GitRevision, Error> {
        GitRevision::try_new(text.to_string()).map_err(|_| anyhow!("{text:?} is not a revision"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GitRev {
    pub commit: GitObjectId,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GitDiff {
    pub base: GitObjectId,
    pub head: GitObjectId,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GitDiffToDisk {
    pub base: GitObjectId,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(untagged)]
pub enum LocalSelection {
    Diff(GitDiff),
    DiffToDisk(GitDiffToDisk),
    Rev(GitRev),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DiffBase {
    Base,
    MergeBase,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DiffRange {
    pub base: GitRevision,
    pub head: GitRevision,
    pub from: DiffBase,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GitDiffSpec {
    ToDisk { base: GitRevision },
    Range(DiffRange),
}

impl GitDiffSpec {
    pub fn parse(diff: &str) -> Result<GitDiffSpec, Error> {
        let not_a_diff = || anyhow!("{:?} is not a diff", diff);
        let revision = |side: &str| {
            GitRevision::parse(if side.is_empty() { "HEAD" } else { side })
                .map_err(|_| not_a_diff())
        };
        for (separator, from) in [("...", DiffBase::MergeBase), ("..", DiffBase::Base)] {
            if let Some((base, head)) = diff.split_once(separator) {
                if head.contains(separator) || (base.is_empty() && head.is_empty()) {
                    return Err(not_a_diff());
                }
                return Ok(GitDiffSpec::Range(DiffRange {
                    base: revision(base)?,
                    head: revision(head)?,
                    from,
                }));
            }
        }
        Ok(GitDiffSpec::ToDisk {
            base: GitRevision::parse(diff).map_err(|_| not_a_diff())?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Line {
    Unchanged(Vec<u8>),
    Added(Vec<u8>),
    Removed(Vec<u8>),
}

impl Line {
    pub fn prefix(&self) -> char {
        match self {
            Line::Unchanged(_) => ' ',
            Line::Added(_) => '+',
            Line::Removed(_) => '-',
        }
    }

    pub fn render(&self) -> String {
        let (Line::Unchanged(text) | Line::Added(text) | Line::Removed(text)) = self;
        format!("{}{}", self.prefix(), String::from_utf8_lossy(text))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Hunk {
    pub path: SourcePath,
    pub old: Option<LineRange>,
    pub new: Option<LineRange>,
    pub section: String,
    pub lines: NonEmpty<Line>,
}

impl Hunk {
    pub(crate) fn new(
        path: &SourcePath,
        header: HunkHeader,
        section: String,
        lines: NonEmpty<Line>,
    ) -> Hunk {
        let [old, new] = [
            (header.before_hunk_start, header.before_hunk_len),
            (header.after_hunk_start, header.after_hunk_len),
        ]
        .map(|(start, len)| {
            let len = NonZeroUsize::new(len as usize)?;
            let start = NonZeroUsize::new(start as usize)?;
            Some(
                LineRange::new(
                    start,
                    start
                        .checked_add(len.get() - 1)
                        .expect("a hunk ends at a line usize can hold"),
                )
                .expect("len lines from start end at or after start"),
            )
        });
        Hunk {
            path: path.clone(),
            old,
            new,
            section,
            lines,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileDiff {
    pub path: SourcePath,
    pub hunks: NonEmpty<Hunk>,
}

impl FileDiff {
    pub fn added(path: &SourcePath, text: &str) -> Option<FileDiff> {
        let lines = text
            .lines()
            .map(|line| Line::Added(line.as_bytes().to_vec()))
            .collect();
        NonEmpty::from_vec(lines).map(|lines: NonEmpty<Line>| FileDiff {
            path: path.clone(),
            hunks: NonEmpty::new(
                Hunk {
                    path: path.clone(),
                    old: None,
                    new: Some(
                        LineRange::new(NonZeroUsize::MIN, lines.len())
                            .expect("a file of lines ends at or after line 1"),
                    ),
                    section: String::new(),
                    lines,
                },
                Vec::new(),
            ),
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Diff {
    pub files: Vec<FileDiff>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Side {
    Absent,
    Present(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContentPair {
    pub path: SourcePath,
    pub old: Side,
    pub new: String,
}

/// The files of a working tree, as git sees them.
pub struct WorkingTree {
    pub tracked: Vec<SourcePath>,
    pub untracked: Vec<SourcePath>,
}

type TreeEntries = BTreeMap<Vec<u8>, (gix::objs::tree::EntryMode, gix::hash::ObjectId)>;

#[derive(Debug, Clone)]
pub struct GitRepo {
    repo: gix::Repository,
    subfolder: Option<PathBuf>,
    root: PathBuf,
}

impl GitRepo {
    pub fn open(root: &Path) -> Result<GitRepo, Error> {
        GitRepo::discover(root)?
            .ok_or_else(|| anyhow!("{} is not in a git repository", root.display()))
    }

    pub fn discover(root: &Path) -> Result<Option<GitRepo>, Error> {
        let repo = match gix::discover(root) {
            Ok(repo) => repo,
            Err(e) if e.is_not_found() => return Ok(None),
            Err(e) => return Err(e.into()),
        };
        let workdir = repo
            .workdir()
            .ok_or_else(|| anyhow!("{} is in a bare repository", root.display()))?;
        let top = std::fs::canonicalize(workdir)?;
        let resolved = std::fs::canonicalize(root)?;
        let relative = resolved.strip_prefix(&top).map_err(|_| {
            anyhow!(
                "{} is not in the subpath of {}",
                resolved.display(),
                top.display()
            )
        })?;
        let subfolder = if relative.as_os_str().is_empty() {
            None
        } else {
            Some(relative.to_path_buf())
        };
        Ok(Some(GitRepo {
            repo,
            subfolder,
            root: resolved,
        }))
    }

    fn commit(&self, revision: &str) -> Result<gix::Commit<'_>, Error> {
        self.repo
            .rev_parse_single(revision)
            .and_then(|id| id.object())
            .and_then(|object| object.peel_to_commit())
            .with_context(|| format!("{:?} is not a commit", revision))
    }

    fn commit_tree(&self, commit: &GitObjectId) -> Result<gix::Tree<'_>, Error> {
        Ok(self.repo.find_commit(commit)?.tree()?)
    }

    fn root_tree(&self, commit: &GitObjectId) -> Result<Option<gix::Tree<'_>>, Error> {
        let tree = self.commit_tree(commit)?;
        let Some(subfolder) = &self.subfolder else {
            return Ok(Some(tree));
        };
        let Some(entry) = tree.lookup_entry_by_path(subfolder)? else {
            return Ok(None);
        };
        if !entry.mode().is_tree() {
            bail!(
                "{} is a {} in {commit}, not a directory",
                subfolder.display(),
                entry.mode().as_str()
            );
        }
        Ok(Some(entry.object()?.try_into_tree()?))
    }

    pub fn read_file_text(
        &self,
        commit: &GitObjectId,
        path: &SourcePath,
    ) -> Result<String, ReadTextError> {
        let contents = (|| -> Result<Vec<u8>, Error> {
            let missing = || anyhow!("no such file in {commit}");
            let tree = self.root_tree(commit)?.ok_or_else(missing)?;
            let entry = tree
                .lookup_entry_by_path(path.to_str())?
                .ok_or_else(missing)?;
            if !entry.mode().is_blob() {
                bail!("a {} in {commit}, not a file", entry.mode().as_str());
            }
            Ok(entry.object()?.try_into_blob()?.detach().data)
        })()?;
        Ok(String::from_utf8(contents)?)
    }
}

fn tree_entries(tree: Option<&gix::Tree<'_>>) -> Result<TreeEntries, Error> {
    let Some(tree) = tree else {
        return Ok(TreeEntries::new());
    };
    Ok(tree
        .traverse()
        .breadthfirst
        .files()?
        .into_iter()
        .filter(|entry| !entry.mode.is_tree())
        .map(|entry| (Vec::from(entry.filepath), (entry.mode, entry.oid)))
        .collect())
}

fn is_git_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | b'\x0b' | b'\x0c' | b'\r')
}

fn function_line(line: &[u8]) -> Option<&[u8]> {
    let end = line.iter().rposition(|&byte| !is_git_space(byte))? + 1;
    let first = line[0];
    if first.is_ascii_alphabetic() || first == b'_' || first == b'$' {
        Some(&line[..end.min(80)])
    } else {
        None
    }
}

struct Collect(Vec<(HunkHeader, Vec<DiffLineKind>)>);

impl ConsumeHunk for Collect {
    type Out = Vec<(HunkHeader, Vec<DiffLineKind>)>;

    fn consume_hunk(
        &mut self,
        header: HunkHeader,
        lines: &[(DiffLineKind, &[u8])],
    ) -> std::io::Result<()> {
        self.0
            .push((header, lines.iter().map(|(kind, _)| *kind).collect()));
        Ok(())
    }

    fn finish(self) -> Self::Out {
        self.0
    }
}

fn changed_file(
    path: &SourcePath,
    old: &str,
    new: &str,
    context: u32,
    inter_hunk_context: u32,
) -> Result<Option<FileDiff>, Error> {
    use gix::diff::blob::{Algorithm, InternedInput, UnifiedDiff, unified_diff::ContextSize};

    let input = InternedInput::new(old, new);
    let mut diff = gix::diff::blob::Diff::compute(Algorithm::Myers, &input);
    diff.postprocess_lines(&input);
    let emitted = UnifiedDiff::new(
        &diff,
        &input,
        Collect(Vec::new()),
        ContextSize::symmetrical(context),
    )
    .consume()?;

    let mut joined: Vec<(HunkHeader, Vec<DiffLineKind>)> = Vec::new();
    for (header, kinds) in emitted {
        if let Some((last, last_kinds)) = joined.last_mut() {
            let gap = header.before_hunk_start - (last.before_hunk_start + last.before_hunk_len);
            if gap <= inter_hunk_context {
                last_kinds.extend(std::iter::repeat_n(DiffLineKind::Context, gap as usize));
                last_kinds.extend(kinds);
                last.before_hunk_len += gap + header.before_hunk_len;
                last.after_hunk_len += gap + header.after_hunk_len;
                continue;
            }
        }
        joined.push((header, kinds));
    }

    let text = |token| input.interner[token];
    let mut hunks = Vec::new();
    let mut function: &[u8] = &[];
    let mut searched_to: i64 = -1;
    for (header, kinds) in joined {
        let old_start = header.before_hunk_start - 1;
        let new_start = header.after_hunk_start - 1;
        for l in ((searched_to + 1)..i64::from(old_start)).rev() {
            if let Some(found) = function_line(text(input.before[l as usize]).as_bytes()) {
                function = found;
                break;
            }
        }
        searched_to = i64::from(old_start) - 1;
        let section = String::from_utf8_lossy(function).trim().to_string();
        let line_text = |token| {
            let line = text(token);
            line.strip_suffix('\n').unwrap_or(line).as_bytes().to_vec()
        };
        let mut before_index = old_start;
        let mut after_index = new_start;
        let mut lines = Vec::new();
        for kind in kinds {
            lines.push(match kind {
                DiffLineKind::Context => {
                    let line = Line::Unchanged(line_text(input.after[after_index as usize]));
                    before_index += 1;
                    after_index += 1;
                    line
                }
                DiffLineKind::Add => {
                    let line = Line::Added(line_text(input.after[after_index as usize]));
                    after_index += 1;
                    line
                }
                DiffLineKind::Remove => {
                    let line = Line::Removed(line_text(input.before[before_index as usize]));
                    before_index += 1;
                    line
                }
            });
        }
        let Some(lines) = NonEmpty::from_vec(lines) else {
            continue;
        };
        hunks.push(Hunk::new(path, header, section, lines));
    }
    Ok(NonEmpty::from_vec(hunks).map(|hunks| FileDiff {
        path: path.clone(),
        hunks,
    }))
}

/// A scan's files from git: a commit's, or the working tree's as git sees it.
#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone)]
pub enum Git {
    /// The files in `commit`'s tree under the scanned directory.
    Commit { repo: GitRepo, commit: GitObjectId },
    /// `paths` under `root`: the working tree's files as `GitRepo::working_tree` finds them.
    WorkingTree {
        root: PathBuf,
        paths: Vec<SourcePath>,
    },
}

impl Git {
    fn commit_paths(repo: &GitRepo, commit: &GitObjectId) -> Result<Vec<SourcePath>, Error> {
        let entries = tree_entries(repo.root_tree(commit)?.as_ref())?;
        let mut found = Vec::new();
        for (raw, (mode, _)) in entries {
            if !mode.is_blob() {
                continue;
            }
            let text = std::str::from_utf8(&raw)?;
            found.push(SourcePath::new(text).with_context(|| text.to_string())?);
        }
        Ok(found)
    }
}

impl Source for Git {
    fn paths(&self) -> Box<dyn Iterator<Item = Result<SourcePath, Error>> + '_> {
        match self {
            Git::Commit { repo, commit } => match Git::commit_paths(repo, commit) {
                Ok(paths) => Box::new(paths.into_iter().map(Ok)),
                Err(e) => Box::new(std::iter::once(Err(e))),
            },
            Git::WorkingTree { paths, .. } => Box::new(paths.iter().cloned().map(Ok)),
        }
    }

    async fn read_text(&self, path: &SourcePath) -> Result<String, ReadTextError> {
        match self {
            Git::Commit { repo, commit } => repo.read_file_text(commit, path),
            Git::WorkingTree { root, .. } => {
                let contents = std::fs::read(path.on_disk(root)).map_err(Error::from)?;
                Ok(String::from_utf8(contents)?)
            }
        }
    }
}

impl GitRepo {
    pub fn resolve_rev(&self, rev: &GitRevision) -> Result<GitRev, Error> {
        Ok(GitRev {
            commit: self.commit(rev.as_str())?.id.into(),
        })
    }

    pub fn resolve_diff(&self, spec: &GitDiffSpec) -> Result<LocalSelection, Error> {
        let spec = match spec {
            GitDiffSpec::ToDisk { base } => {
                let base = self.commit(base.as_str())?.id;
                return Ok(LocalSelection::DiffToDisk(GitDiffToDisk {
                    base: base.into(),
                }));
            }
            GitDiffSpec::Range(range) => range,
        };
        let base = self.commit(spec.base.as_str())?.id;
        let head = &spec.head;
        let head_id = self.commit(head.as_str())?.id;
        let base = match spec.from {
            DiffBase::Base => base,
            DiffBase::MergeBase => self
                .repo
                .merge_base(base, head_id)
                .ok()
                .flatten()
                .map(|id| id.detach())
                .ok_or_else(|| anyhow!("{} and {head} have no merge base", spec.base))?,
        };
        Ok(LocalSelection::Diff(GitDiff {
            base: base.into(),
            head: head_id.into(),
        }))
    }

    pub fn changes(
        &self,
        selection: &LocalSelection,
        context: u32,
    ) -> Result<Option<Changes>, Error> {
        Ok(match selection {
            LocalSelection::Rev(_) => None,
            LocalSelection::Diff(diff) => Some(Changes {
                diff: self.changes_between_commits(&diff.base, &diff.head, context, 0)?,
                untracked: Vec::new(),
            }),
            LocalSelection::DiffToDisk(to_disk) => {
                Some(self.changes_on_disk_since(&to_disk.base, context)?)
            }
        })
    }

    fn changes_between_commits(
        &self,
        base: &GitObjectId,
        head: &GitObjectId,
        context: u32,
        inter_hunk_context: u32,
    ) -> Result<Diff, Error> {
        let old = tree_entries(self.root_tree(base)?.as_ref())?;
        let new = tree_entries(self.root_tree(head)?.as_ref())?;
        let mut files = Vec::new();
        for (raw, &(mode, _)) in &new {
            if !mode.is_blob() {
                continue;
            }
            let text = std::str::from_utf8(raw)?;
            let path = SourcePath::new(text).with_context(|| text.to_string())?;
            let in_base = old.get(raw).is_some_and(|(old_mode, _)| old_mode.is_blob());
            let texts = (|| -> Result<(String, String), ReadTextError> {
                let old_text = if in_base {
                    self.read_file_text(base, &path)?
                } else {
                    String::new()
                };
                Ok((old_text, self.read_file_text(head, &path)?))
            })();
            match texts {
                Ok((old_text, new_text)) => files.extend(changed_file(
                    &path,
                    &old_text,
                    &new_text,
                    context,
                    inter_hunk_context,
                )?),
                Err(ReadTextError::NotUtf8(e)) => {
                    tracing::debug!("skipping {path}: not UTF-8 text: {e}");
                }
                Err(ReadTextError::Read(e)) => return Err(e.context(path.to_string())),
            }
        }
        Ok(Diff { files })
    }

    /// The working tree's files under the scanned directory, as git sees them: each tracked
    /// file the index lists that is a regular file on disk, ignore rules never applying to it,
    /// and each untracked regular file git's ignore rules leave in. Git's rules are the
    /// repository's `.gitignore` files, `.git/info/exclude` and `core.excludesFile` from its git
    /// configuration, or `$XDG_CONFIG_HOME/git/ignore` when that is unset.
    pub fn working_tree(&self) -> Result<WorkingTree, Error> {
        let index = self.repo.index_or_empty()?;
        let mut tracked = Vec::new();
        for entry in index.entries() {
            let text = std::str::from_utf8(entry.path(&index))?;
            let Some(path) = self.in_scanned_directory(text)? else {
                continue;
            };
            let file = path.on_disk(&self.root);
            match std::fs::symlink_metadata(&file) {
                Ok(metadata) if metadata.is_file() => tracked.push(path),
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(anyhow!("{}: {e}", file.display())),
            }
        }
        let options = self
            .repo
            .dirwalk_options()?
            .emit_untracked(gix::dir::walk::EmissionMode::Matching)
            .emit_ignored(None)
            .emit_tracked(false);
        let mut collect = gix::dir::walk::delegate::Collect::default();
        self.repo.dirwalk(
            &index,
            std::iter::empty::<&gix::bstr::BStr>(),
            &AtomicBool::new(false),
            options,
            &mut collect,
        )?;
        let mut untracked = Vec::new();
        for (entry, _) in collect.into_entries_by_path() {
            if !matches!(entry.status, gix::dir::entry::Status::Untracked)
                || !matches!(entry.disk_kind, Some(gix::dir::entry::Kind::File))
            {
                continue;
            }
            if let Some(path) = self.in_scanned_directory(std::str::from_utf8(&entry.rela_path)?)? {
                untracked.push(path);
            }
        }
        tracked.sort();
        untracked.sort();
        Ok(WorkingTree { tracked, untracked })
    }

    /// `text`, a path from the top of the repository, as a path in the scanned directory, or
    /// `None` when it is outside it.
    fn in_scanned_directory(&self, text: &str) -> Result<Option<SourcePath>, Error> {
        let built = match &self.subfolder {
            Some(subfolder) => SourcePath::from_base(subfolder, Path::new(text)),
            None => SourcePath::new(text),
        };
        match built {
            Ok(path) => Ok(Some(path)),
            Err(SourcePathError::Outside) => Ok(None),
            Err(e) => Err(Error::from(e).context(text.to_string())),
        }
    }

    fn changes_on_disk_since(&self, base: &GitObjectId, context: u32) -> Result<Changes, Error> {
        let old = tree_entries(self.root_tree(base)?.as_ref())?;
        let WorkingTree { tracked, untracked } = self.working_tree()?;
        let mut files = Vec::new();
        for path in tracked {
            let file = path.on_disk(&self.root);
            let in_base = old
                .get(path.to_str().as_bytes())
                .is_some_and(|(old_mode, _)| old_mode.is_blob());
            let texts = (|| -> Result<(String, String), ReadTextError> {
                let old_text = if in_base {
                    self.read_file_text(base, &path)?
                } else {
                    String::new()
                };
                let new = std::fs::read(&file).map_err(Error::from)?;
                Ok((old_text, String::from_utf8(new)?))
            })();
            match texts {
                Ok((old_text, new_text)) => {
                    files.extend(changed_file(&path, &old_text, &new_text, context, 0)?)
                }
                Err(ReadTextError::NotUtf8(e)) => {
                    tracing::debug!("skipping {path}: not UTF-8 text: {e}");
                }
                Err(ReadTextError::Read(e)) => return Err(e.context(path.to_string())),
            }
        }
        let mut kept = Vec::new();
        for path in untracked {
            let joined = path.on_disk(&self.root);
            match std::fs::canonicalize(&joined) {
                Ok(real) if real == joined => kept.push(path),
                Ok(_) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(anyhow!("{}: {e}", joined.display())),
            }
        }
        Ok(Changes {
            diff: Diff { files },
            untracked: kept,
        })
    }
}

pub fn diff_contents(
    pairs: &[ContentPair],
    context: u32,
    inter_hunk_context: u32,
) -> Result<Diff, Error> {
    let mut files = Vec::new();
    for pair in pairs {
        let old = match &pair.old {
            Side::Absent => "",
            Side::Present(old) => old.as_str(),
        };
        files.extend(changed_file(
            &pair.path,
            old,
            &pair.new,
            context,
            inter_hunk_context,
        )?);
    }
    Ok(Diff { files })
}

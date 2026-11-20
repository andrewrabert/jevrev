use anyhow::{Context, anyhow};
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::num::NonZeroUsize;
use std::path::Path;
use std::sync::atomic::AtomicBool;

use gix::diff::blob::unified_diff::{ConsumeHunk, DiffLineKind, HunkHeader};

use crate::Error;
use crate::gitignore::Spec;
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
    pub path: String,
    pub old: Option<LineRange>,
    pub new: Option<LineRange>,
    pub section: String,
    pub lines: NonEmpty<Line>,
}

impl Hunk {
    pub(crate) fn new(
        path: &str,
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
            path: path.to_string(),
            old,
            new,
            section,
            lines,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileDiff {
    pub path: String,
    pub hunks: NonEmpty<Hunk>,
}

impl FileDiff {
    pub fn added(path: &str, text: &str) -> Option<FileDiff> {
        let lines = text
            .lines()
            .map(|line| Line::Added(line.as_bytes().to_vec()))
            .collect();
        NonEmpty::from_vec(lines).map(|lines: NonEmpty<Line>| FileDiff {
            path: path.to_string(),
            hunks: NonEmpty::new(
                Hunk {
                    path: path.to_string(),
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
    Present(Vec<u8>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContentPair {
    pub path: String,
    pub old: Side,
    pub new: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IgnoreFile {
    pub directory: String,
    pub contents: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct AboveSource {
    pub prefix: String,
    pub ignore_files: Vec<IgnoreFile>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IgnoredFiles {
    Included,
    Excluded,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadFrom<'commit> {
    Disk,
    Commit(&'commit GitObjectId),
}

type TreeEntries = BTreeMap<Vec<u8>, (gix::objs::tree::EntryMode, gix::hash::ObjectId)>;

#[derive(Debug, Clone)]
pub struct GitRepo {
    repo: gix::Repository,
    top: std::path::PathBuf,
    prefix: String,
}

impl GitRepo {
    pub fn open(root: &Path) -> Result<GitRepo, Error> {
        GitRepo::discover(root)?
            .ok_or_else(|| anyhow!("{} is not in a git repository", root.display()))
    }

    pub fn discover(root: &Path) -> Result<Option<GitRepo>, Error> {
        let Ok(repo) = gix::discover(root) else {
            return Ok(None);
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
        let relative = relative
            .to_str()
            .ok_or_else(|| anyhow!("{}: the path is not valid UTF-8", root.display()))?;
        let prefix = if relative.is_empty() {
            String::new()
        } else {
            format!("{relative}/")
        };
        Ok(Some(GitRepo { repo, top, prefix }))
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
        if self.prefix.is_empty() {
            return Ok(Some(tree));
        }
        Ok(tree
            .lookup_entry_by_path(self.prefix.trim_end_matches('/'))
            .ok()
            .flatten()
            .filter(|entry| entry.mode().is_tree())
            .and_then(|entry| entry.object().ok())
            .and_then(|object| object.try_into_tree().ok()))
    }

    fn blob(&self, id: gix::hash::ObjectId) -> Result<Vec<u8>, Error> {
        let mut blob = self.repo.find_blob(id)?;
        Ok(std::mem::take(&mut blob.data))
    }

    fn relative<'p>(&self, path: &'p str) -> Option<&'p str> {
        path.strip_prefix(self.prefix.as_str())
    }
}

fn blob_at(tree: &gix::Tree<'_>, path: &str) -> Option<Vec<u8>> {
    let entry = tree.lookup_entry_by_path(Path::new(path)).ok()??;
    let mut blob = entry.object().ok()?.try_into_blob().ok()?;
    Some(std::mem::take(&mut blob.data))
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

fn is_binary(data: &[u8]) -> bool {
    data[..data.len().min(8000)].contains(&0)
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
    path: &str,
    old: &[u8],
    new: &[u8],
    context: u32,
    inter_hunk_context: u32,
) -> Option<FileDiff> {
    use gix::diff::blob::{Algorithm, InternedInput, UnifiedDiff, unified_diff::ContextSize};

    if is_binary(old) || is_binary(new) {
        return None;
    }
    let input = InternedInput::new(old, new);
    let mut diff = gix::diff::blob::Diff::compute(Algorithm::Myers, &input);
    diff.postprocess_lines(&input);
    let emitted = UnifiedDiff::new(
        &diff,
        &input,
        Collect(Vec::new()),
        ContextSize::symmetrical(context),
    )
    .consume()
    .ok()?;

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
            if let Some(found) = function_line(text(input.before[l as usize])) {
                function = found;
                break;
            }
        }
        searched_to = i64::from(old_start) - 1;
        let section = str::trim(std::str::from_utf8(function).ok()?).to_string();
        let line_text = |token| {
            let line = text(token);
            line.strip_suffix(b"\n").unwrap_or(line).to_vec()
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
        hunks.push(Hunk::new(path, header, section, NonEmpty::from_vec(lines)?));
    }
    NonEmpty::from_vec(hunks).map(|hunks| FileDiff {
        path: path.to_string(),
        hunks,
    })
}

#[derive(Debug, Clone)]
pub struct CommitFiles {
    repo: GitRepo,
    commit: GitObjectId,
}

impl CommitFiles {
    pub fn new(repo: GitRepo, commit: GitObjectId) -> CommitFiles {
        CommitFiles { repo, commit }
    }

    pub fn paths(&self) -> Result<Vec<String>, Error> {
        let entries = tree_entries(self.repo.root_tree(&self.commit)?.as_ref())?;
        Ok(entries
            .into_iter()
            .filter(|(_, (mode, _))| mode.is_blob())
            .filter_map(|(path, _)| {
                String::from_utf8(path)
                    .inspect_err(|e| {
                        tracing::info!("skipping {}: not a UTF-8 path", e.as_bytes().escape_ascii())
                    })
                    .ok()
            })
            .collect())
    }

    pub fn read(&self, path: &str) -> Result<Vec<u8>, Error> {
        let missing = || anyhow!("{path} is not in {}", self.commit);
        let tree = self.repo.root_tree(&self.commit)?.ok_or_else(missing)?;
        blob_at(&tree, path).ok_or_else(missing)
    }
}

fn read_if_file(path: &Path) -> Option<Vec<u8>> {
    std::fs::read(path).ok()
}

pub fn global_excludes() -> Vec<Spec> {
    vec![Spec::global()]
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

    pub fn diff_commits(
        &self,
        base: &GitObjectId,
        head: &GitObjectId,
        context: u32,
        inter_hunk_context: u32,
    ) -> Result<Diff, Error> {
        let old = tree_entries(self.root_tree(base)?.as_ref())?;
        let new = tree_entries(self.root_tree(head)?.as_ref())?;
        let mut files = Vec::new();
        for (raw, &(mode, id)) in &new {
            let Ok(path) = std::str::from_utf8(raw) else {
                tracing::info!("skipping {}: not a UTF-8 path", raw.escape_ascii());
                continue;
            };
            if !mode.is_blob() {
                continue;
            }
            let old_side = old
                .get(raw)
                .copied()
                .filter(|(old_mode, _)| old_mode.is_blob());
            let changed = match old_side {
                Some((_, old_id)) => changed_file(
                    path,
                    &self.blob(old_id)?,
                    &self.blob(id)?,
                    context,
                    inter_hunk_context,
                ),
                None => changed_file(path, &[], &self.blob(id)?, context, inter_hunk_context),
            };
            files.extend(changed);
        }
        Ok(Diff { files })
    }

    pub fn diff_work_tree(&self, base: &GitObjectId, context: u32) -> Result<Diff, Error> {
        let old = tree_entries(self.root_tree(base)?.as_ref())?;
        let index = self.repo.index_or_empty()?;
        let tracked: BTreeSet<&[u8]> = index
            .entries()
            .iter()
            .filter_map(|entry| entry.path(&index).strip_prefix(self.prefix.as_bytes()))
            .collect();
        let directory = self.top.join(&self.prefix);
        let mut files = Vec::new();
        for raw in tracked {
            let Ok(path) = std::str::from_utf8(raw) else {
                tracing::info!("skipping {}: not a UTF-8 path", raw.escape_ascii());
                continue;
            };
            let file = directory.join(path);
            let metadata = match std::fs::symlink_metadata(&file) {
                Ok(metadata) => metadata,
                Err(e) => {
                    tracing::info!("skipping {path}: {e}");
                    continue;
                }
            };
            if !metadata.is_file() {
                continue;
            }
            let new = std::fs::read(&file).with_context(|| file.display().to_string())?;
            let old_side = old
                .get(raw)
                .copied()
                .filter(|(old_mode, _)| old_mode.is_blob());
            let changed = match old_side {
                Some((_, old_id)) => changed_file(path, &self.blob(old_id)?, &new, context, 0),
                None => changed_file(path, &[], &new, context, 0),
            };
            files.extend(changed);
        }
        Ok(Diff { files })
    }

    pub fn untracked_files(&self, ignored: IgnoredFiles) -> Result<Vec<String>, Error> {
        let index = self.repo.index_or_empty()?;
        let mut found = Vec::new();
        match ignored {
            IgnoredFiles::Included => {
                let tracked: HashSet<&[u8]> = index
                    .entries()
                    .iter()
                    .map(|entry| &**entry.path(&index))
                    .collect();
                for full in crate::walk::walk(&self.top)? {
                    if tracked.contains(full.as_bytes()) {
                        continue;
                    }
                    if let Some(path) = self.relative(&full) {
                        found.push(path.to_string());
                    }
                }
            }
            IgnoredFiles::Excluded => {
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
                for (entry, _) in collect.into_entries_by_path() {
                    if !matches!(entry.status, gix::dir::entry::Status::Untracked)
                        || !matches!(entry.disk_kind, Some(gix::dir::entry::Kind::File))
                    {
                        continue;
                    }
                    let Ok(full) = std::str::from_utf8(&entry.rela_path) else {
                        tracing::info!(
                            "skipping {}: not a UTF-8 path",
                            entry.rela_path.escape_ascii()
                        );
                        continue;
                    };
                    if let Some(path) = self.relative(full) {
                        found.push(path.to_string());
                    }
                }
            }
        }
        let base = self.top.join(&self.prefix);
        Ok(found
            .into_iter()
            .filter(|path| {
                let joined = base.join(path);
                std::fs::canonicalize(&joined).is_ok_and(|real| real == joined)
            })
            .collect())
    }

    pub fn ignore_files_above(&self, at: ReadFrom<'_>) -> Result<AboveSource, Error> {
        let parts: Vec<&str> = if self.prefix.is_empty() {
            Vec::new()
        } else {
            self.prefix.trim_end_matches('/').split('/').collect()
        };
        let tree = match at {
            ReadFrom::Disk => None,
            ReadFrom::Commit(commit) => Some(self.commit_tree(commit)?),
        };
        let mut ignore_files = Vec::new();
        for n in 0..parts.len() {
            let directory = parts[..n].join("/");
            for name in [".gitignore", ".ignore"] {
                let path = if directory.is_empty() {
                    name.to_string()
                } else {
                    format!("{directory}/{name}")
                };
                let contents = match &tree {
                    None => {
                        let file = self.top.join(&path);
                        let regular = std::fs::symlink_metadata(&file).is_ok_and(|m| m.is_file());
                        if regular { read_if_file(&file) } else { None }
                    }
                    Some(tree) => blob_at(tree, &path),
                };
                if let Some(contents) = contents {
                    ignore_files.push(IgnoreFile {
                        directory: directory.clone(),
                        contents,
                    });
                }
            }
        }
        Ok(AboveSource {
            prefix: self.prefix.clone(),
            ignore_files,
        })
    }

    pub fn excludes(&self) -> Vec<Spec> {
        let mut found = global_excludes();
        found.extend(
            read_if_file(&self.repo.git_dir().join("info").join("exclude"))
                .map(|c| Spec::from_bytes(&c)),
        );
        found
    }
}

pub fn diff_contents(pairs: &[ContentPair], context: u32, inter_hunk_context: u32) -> Diff {
    let mut files = Vec::new();
    for pair in pairs {
        let old: &[u8] = match &pair.old {
            Side::Absent => &[],
            Side::Present(old) => old,
        };
        files.extend(changed_file(
            &pair.path,
            old,
            &pair.new,
            context,
            inter_hunk_context,
        ));
    }
    Diff { files }
}

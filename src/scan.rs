pub const DIFF_CONTEXT: u32 = 20;

static RULE_ID: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
    regex::Regex::new(r"^[^#]+#[1-9][0-9]*$").expect("a valid rule id pattern")
});

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize, validator::Validate)]
pub struct Finding {
    #[validate(length(min = 1))]
    pub file: String,
    #[serde(flatten)]
    pub line_range: LineRange,
    #[validate(regex(path = *RULE_ID))]
    pub rule: String,
    #[validate(range(min = 0.0, max = 1.0))]
    pub probability: f64,
    pub flags: String,
    pub violation: String,
    pub accepted: String,
    pub state: StateKind,
}

use futures::StreamExt;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};

use crate::Error;
use crate::files::Files;
use crate::git::{Diff, FileDiff, Line};
use crate::gitignore::{Check, Pattern, Spec};
use crate::ignore_rules::{Fate, Ignore};
use crate::jev::api::{Client, NoulAnswer};
use crate::jev::ask;
use crate::languages::{self, Syntax, TestMarker};
use crate::lines::LineRange;
use crate::non_empty::NonEmpty;
use crate::rules::{Conditions, Language, RuleSet};
use crate::state::{Body, DiffHunk, Extent, State, StateKind};

pub enum IgnoreRules {
    Apply(Ignore),
    Off,
}

pub struct ScanScope {
    pub ignore: IgnoreRules,
    pub include: Vec<String>,
    pub exclude: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Changes {
    pub diff: Diff,
    pub untracked: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Scanned {
    pub states: Vec<State>,
    pub rule_sets: BTreeMap<PathBuf, RuleSet>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Reviewed {
    pub findings: Vec<Finding>,
    pub failures: Vec<String>,
}

#[derive(Debug, thiserror::Error)]
pub enum ScanError {
    #[error("{0}")]
    Jev(String),
    #[error(transparent)]
    Source(#[from] Error),
}

enum PathFilter {
    Any,
    Only(Spec),
}

impl PathFilter {
    fn new(patterns: Vec<Pattern>) -> PathFilter {
        if patterns.is_empty() {
            PathFilter::Any
        } else {
            PathFilter::Only(Spec::new(patterns))
        }
    }

    fn admits(&self, path: &str) -> bool {
        match self {
            PathFilter::Any => true,
            PathFilter::Only(spec) => spec.check(path) == Check::Included,
        }
    }
}

struct Condition {
    languages: Vec<Language>,
    patterns: PathFilter,
    exists: Vec<Spec>,
}

impl Condition {
    fn holds_among(&self, paths: &[String]) -> bool {
        self.exists
            .iter()
            .all(|spec| paths.iter().any(|path| spec.check(path) == Check::Included))
    }

    fn matches(&self, file: &File) -> bool {
        let language =
            self.languages.is_empty() || file.languages.iter().any(|l| self.languages.contains(l));
        language && self.patterns.admits(&file.path)
    }
}

struct SetConditions {
    path: PathBuf,
    conditions: Vec<Condition>,
}

fn yaml_files(directory: &Path) -> Vec<PathBuf> {
    let mut found: Vec<PathBuf> = ignore::WalkBuilder::new(directory)
        .standard_filters(false)
        .build()
        .flatten()
        .filter(|entry| entry.depth() > 0)
        .filter(|entry| {
            entry
                .file_name()
                .to_str()
                .is_some_and(|n| n.ends_with(".yaml"))
        })
        .map(ignore::DirEntry::into_path)
        .collect();
    found.sort_by(|a, b| a.components().cmp(b.components()));
    found
}

fn rule_sets(
    rules: &[PathBuf],
    loaded: &mut BTreeMap<PathBuf, RuleSet>,
) -> Result<Vec<SetConditions>, ScanError> {
    let mut sets: Vec<SetConditions> = Vec::new();
    let mut seen: HashSet<PathBuf> = HashSet::new();
    for directory in rules {
        let directory = std::fs::canonicalize(directory)
            .or_else(|_| std::path::absolute(directory))
            .unwrap_or_else(|_| directory.clone());
        if !directory.is_dir() {
            return Err(ScanError::Jev(format!(
                "rule sets directory {} is not a directory",
                directory.display()
            )));
        }
        for path in yaml_files(&directory) {
            if !loaded.contains_key(&path) {
                let parsed = RuleSet::load(&path).map_err(|e| ScanError::Jev(format!("{e:#}")))?;
                loaded.insert(path.clone(), parsed);
            }
            let Conditions::Given(when) = &loaded[&path].when else {
                continue;
            };
            if !seen.insert(path.clone()) {
                continue;
            }
            sets.push(SetConditions {
                path,
                conditions: when
                    .iter()
                    .map(|w| Condition {
                        languages: w.language.clone(),
                        patterns: PathFilter::new(w.matches.clone()),
                        exists: w
                            .exists
                            .iter()
                            .map(|pattern| Spec::new(vec![pattern.clone()]))
                            .collect(),
                    })
                    .collect(),
            });
        }
    }
    Ok(sets)
}

fn sets_among(sets: Vec<SetConditions>, paths: &[String]) -> Vec<SetConditions> {
    sets.into_iter()
        .map(|set| SetConditions {
            path: set.path,
            conditions: set
                .conditions
                .into_iter()
                .filter(|c| c.holds_among(paths))
                .collect(),
        })
        .filter(|set| !set.conditions.is_empty())
        .collect()
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct File {
    path: String,
    languages: BTreeSet<Language>,
}

fn touched_ranges(language: Language, text: &str, file_diff: &FileDiff) -> Syntax<Vec<LineRange>> {
    let after = match languages::ranges(language, text.as_bytes()) {
        Syntax::Valid(ranges) => ranges,
        Syntax::Invalid => return Syntax::Invalid,
    };
    let text_lines = text.lines().collect::<Vec<_>>();
    let mut old_text: Vec<u8> = Vec::new();
    let mut old_lines: usize = 0;
    let mut new_line_of_old: HashMap<NonZeroUsize, NonZeroUsize> = HashMap::new();
    let mut push_old = |old_text: &mut Vec<u8>, line: &[u8]| -> NonZeroUsize {
        old_text.extend_from_slice(line);
        old_text.push(b'\n');
        let number = NonZeroUsize::MIN.saturating_add(old_lines);
        old_lines += 1;
        number
    };
    let mut touched: HashSet<NonZeroUsize> = HashSet::new();
    let mut next_new = NonZeroUsize::MIN;
    for hunk in file_diff.hunks.iter() {
        if let Some(new) = hunk.new {
            while next_new < new.start() {
                let old = push_old(&mut old_text, text_lines[next_new.get() - 1].as_bytes());
                new_line_of_old.insert(old, next_new);
                next_new = next_new
                    .checked_add(1)
                    .expect("a line number fits in usize");
            }
        }
        for line in hunk.lines.iter() {
            match line {
                Line::Unchanged(text) => {
                    let old = push_old(&mut old_text, text);
                    new_line_of_old.insert(old, next_new);
                    next_new = next_new
                        .checked_add(1)
                        .expect("a line number fits in usize");
                }
                Line::Added(_) => {
                    touched.insert(next_new);
                    next_new = next_new
                        .checked_add(1)
                        .expect("a line number fits in usize");
                }
                Line::Removed(text) => {
                    push_old(&mut old_text, text);
                }
            }
        }
    }
    while next_new.get() <= text_lines.len() {
        let old = push_old(&mut old_text, text_lines[next_new.get() - 1].as_bytes());
        new_line_of_old.insert(old, next_new);
        next_new = next_new
            .checked_add(1)
            .expect("a line number fits in usize");
    }

    let old_ranges = match languages::ranges(language, &old_text) {
        Syntax::Valid(ranges) => ranges,
        Syntax::Invalid => {
            tracing::info!(
                "{}: the old side does not parse as {language:?}, so every range counts as touched",
                file_diff.path
            );
            return Syntax::Valid(after);
        }
    };
    for range in old_ranges {
        if range
            .line_numbers()
            .any(|n| !new_line_of_old.contains_key(&n))
        {
            touched.extend(
                range
                    .line_numbers()
                    .filter_map(|n| new_line_of_old.get(&n).copied()),
            );
        }
    }
    Syntax::Valid(
        after
            .into_iter()
            .filter(|range| range.line_numbers().any(|n| touched.contains(&n)))
            .collect(),
    )
}

fn test_start(lines: &[&str], marker: TestMarker) -> Option<NonZeroUsize> {
    let TestMarker::Line(marker) = marker else {
        return None;
    };
    let row = lines.iter().position(|line| *line == marker)?;
    Some(NonZeroUsize::MIN.saturating_add(row))
}

fn language_states(
    file: &File,
    sets: &NonEmpty<PathBuf>,
    language: Language,
    text: &str,
    file_diff: Option<&FileDiff>,
) -> Vec<State> {
    let ranges = match file_diff {
        None => languages::ranges(language, text.as_bytes()),
        Some(file_diff) => touched_ranges(language, text, file_diff),
    };
    let ranges = match ranges {
        Syntax::Valid(ranges) => ranges,
        Syntax::Invalid => {
            tracing::info!(
                "{}: does not parse as {language:?}, so it has no {language:?} states",
                file.path
            );
            return Vec::new();
        }
    };
    let lines = text.lines().collect::<Vec<_>>();
    let tests = test_start(&lines, languages::test_marker(language));
    let mut states = Vec::new();
    for range in ranges {
        let range = match tests {
            Some(tests) => match range.before(tests) {
                Some(range) => range,
                None => continue,
            },
            None => range,
        };
        let part = &lines[range.start().get() - 1..range.end().get()];
        if part.iter().all(|line| line.trim().is_empty()) {
            continue;
        }
        states.push(State {
            file: file.path.clone(),
            line_range: range,
            sets: sets.clone(),
            body: Body::Language {
                language,
                text: part.join("\n"),
            },
        });
    }
    states
}

fn header_side(range: Option<LineRange>, lines: usize) -> String {
    match range {
        Some(range) => format!("{},{lines}", range.start()),
        None => "0,0".to_string(),
    }
}

fn hunk_states(sets: &NonEmpty<PathBuf>, file_diff: &FileDiff) -> Vec<State> {
    let hunks = file_diff.hunks.iter().filter_map(|hunk| {
        let new = hunk.new?;
        let count = |side: fn(&Line) -> bool| hunk.lines.iter().filter(|line| side(line)).count();
        let old_lines = count(|line| matches!(line, Line::Unchanged(_) | Line::Removed(_)));
        let new_lines = count(|line| matches!(line, Line::Unchanged(_) | Line::Added(_)));
        let header = format!(
            "@@ -{} +{} @@ {}",
            header_side(hunk.old, old_lines),
            header_side(Some(new), new_lines),
            hunk.section
        );
        Some((
            new,
            DiffHunk {
                header: header.trim_end().to_string(),
                lines: hunk.lines.iter().map(Line::render).collect(),
            },
        ))
    });
    let Some(hunks) = NonEmpty::from_vec(hunks.collect()) else {
        return Vec::new();
    };
    let mut states: Vec<State> = hunks
        .iter()
        .map(|(line_range, hunk)| State {
            file: file_diff.path.clone(),
            line_range: *line_range,
            sets: sets.clone(),
            body: Body::Hunk(hunk.clone()),
        })
        .collect();
    states.push(State {
        file: file_diff.path.clone(),
        line_range: hunks.first().0.through(&hunks.last().0),
        sets: sets.clone(),
        body: Body::Hunks(hunks.map(|(_, hunk)| hunk.clone())),
    });
    states
}

fn states_of(
    file: &File,
    sets: &NonEmpty<PathBuf>,
    text: &str,
    file_diff: Option<&FileDiff>,
) -> Vec<State> {
    let mut states = vec![State {
        file: file.path.clone(),
        line_range: LineRange::new(
            NonZeroUsize::MIN,
            NonZeroUsize::new(text.lines().count()).unwrap_or(NonZeroUsize::MIN),
        )
        .expect("a file ends at or after line 1"),
        sets: sets.clone(),
        body: Body::File {
            text: text.to_string(),
        },
    }];
    if let Some(file_diff) = file_diff {
        states.extend(hunk_states(sets, file_diff));
    }
    for language in &file.languages {
        states.extend(language_states(file, sets, *language, text, file_diff));
    }
    states
}

pub struct ScanRun<'s> {
    source: &'s Files,
    rule_sets: BTreeMap<PathBuf, RuleSet>,
    selection: BTreeMap<File, NonEmpty<PathBuf>>,
}

impl<'s> ScanRun<'s> {
    async fn select(
        rules: &[PathBuf],
        source: &'s Files,
        scope: &ScanScope,
        candidates: &[&str],
    ) -> Result<ScanRun<'s>, ScanError> {
        let mut paths = source.paths()?;
        if let IgnoreRules::Apply(ignore) = &scope.ignore {
            paths.retain(|path| ignore.fate(path) == Fate::Kept);
        }
        let included = if scope.include.is_empty() {
            PathFilter::Any
        } else {
            PathFilter::Only(Spec::from_lines(scope.include.iter().map(String::as_str)))
        };
        let excluded = Spec::from_lines(scope.exclude.iter().map(String::as_str));
        let in_scope: HashSet<&str> = paths
            .iter()
            .map(String::as_str)
            .filter(|path| included.admits(path) && excluded.check(path) != Check::Included)
            .collect();
        let mut rule_sets = BTreeMap::new();
        let sets = sets_among(self::rule_sets(rules, &mut rule_sets)?, &paths);
        let mut selection = BTreeMap::new();
        for path in candidates {
            if !in_scope.contains(path) {
                continue;
            }
            let file = File {
                path: path.to_string(),
                languages: languages::detect(path, &source.read(path).await?),
            };
            let selecting = sets
                .iter()
                .filter(|set| set.conditions.iter().any(|c| c.matches(&file)))
                .map(|set| set.path.clone())
                .collect();
            if let Some(selecting) = NonEmpty::from_vec(selecting) {
                selection.insert(file, selecting);
            }
        }
        Ok(ScanRun {
            source,
            rule_sets,
            selection,
        })
    }

    async fn contents(&self, file: &File) -> Result<String, ScanError> {
        String::from_utf8(self.source.read(&file.path).await?)
            .map_err(|e| ScanError::Jev(format!("{} is not UTF-8 text: {e}", file.path)))
    }

    fn scanned(self, states: Vec<State>) -> Scanned {
        let named: HashSet<&PathBuf> = states.iter().flat_map(|s| s.sets.iter()).collect();
        let rule_sets = self
            .rule_sets
            .into_iter()
            .filter(|(path, _)| named.contains(path))
            .collect();
        Scanned { states, rule_sets }
    }
}

pub async fn scan(
    rules: &[PathBuf],
    source: &Files,
    scope: &ScanScope,
    changes: Option<&Changes>,
) -> Result<Scanned, ScanError> {
    let changed: BTreeMap<&str, &FileDiff> = changes
        .iter()
        .flat_map(|changes| &changes.diff.files)
        .map(|file_diff| (file_diff.path.as_str(), file_diff))
        .collect();
    let paths;
    let candidates: Vec<&str> = match changes {
        None => {
            paths = source.paths()?;
            paths.iter().map(String::as_str).collect()
        }
        Some(changes) => changed
            .keys()
            .copied()
            .chain(changes.untracked.iter().map(String::as_str))
            .collect(),
    };
    let run = ScanRun::select(rules, source, scope, &candidates).await?;
    let mut states = Vec::new();
    for (file, sets) in &run.selection {
        let text = match run.contents(file).await {
            Ok(text) => text,
            Err(ScanError::Jev(e)) => {
                tracing::debug!("skipping {e}");
                continue;
            }
            Err(e) => return Err(e),
        };
        let added = changes
            .filter(|_| !changed.contains_key(file.path.as_str()))
            .and_then(|_| FileDiff::added(&file.path, &text));
        let file_diff = changed.get(file.path.as_str()).copied().or(added.as_ref());
        states.extend(states_of(file, sets, &text, file_diff));
    }
    Ok(run.scanned(states))
}

fn file_name(path: &Path) -> String {
    path.file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default()
}

fn ask_failed(state: &State, message: &str) -> String {
    format!(
        "{}:{}-{} {}: {}",
        state.file,
        state.line_range.start(),
        state.line_range.end(),
        state.kind().as_ref(),
        message.trim()
    )
}

async fn ask_one(
    state: &State,
    rule_set: &RuleSet,
    client: &Client,
) -> Result<Vec<NoulAnswer>, String> {
    client
        .ask(&ask::questions(rule_set), &state.render())
        .await
        .map_err(|e| ask_failed(state, &format!("{e:#}")))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Concurrency(NonZeroUsize);

impl Default for Concurrency {
    fn default() -> Concurrency {
        Concurrency(NonZeroUsize::new(8).expect("8 is positive"))
    }
}

impl std::fmt::Display for Concurrency {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(f)
    }
}

impl Concurrency {
    pub fn try_new(value: impl AsRef<std::ffi::OsStr>) -> Result<Concurrency, Error> {
        let value = value.as_ref();
        value
            .to_str()
            .and_then(|text| text.parse().ok())
            .map(Concurrency)
            .ok_or_else(|| anyhow::anyhow!("invalid value: {value:?}"))
    }

    pub fn get(self) -> usize {
        self.0.get()
    }
}

pub async fn review(
    scanned: &Scanned,
    client: &Client,
    concurrency: Concurrency,
) -> Result<Reviewed, ScanError> {
    let asked: Vec<(usize, &State, &PathBuf)> = scanned
        .states
        .iter()
        .enumerate()
        .flat_map(|(n, state)| state.sets.iter().map(move |set| (n, state, set)))
        .collect();
    let results: Vec<Result<Vec<NoulAnswer>, String>> = futures::stream::iter(&asked)
        .map(|(_, state, set)| ask_one(state, &scanned.rule_sets[*set], client))
        .buffered(concurrency.get())
        .collect()
        .await;
    if !results.is_empty() && results.iter().all(Result::is_err) {
        let first = results[0].as_ref().err().cloned().unwrap_or_default();
        return Err(ScanError::Jev(format!(
            "every jev request failed; the first: {first}"
        )));
    }
    let mut found = Vec::new();
    let mut failures = Vec::new();
    let mut reported: HashSet<(usize, String)> = HashSet::new();
    for ((state_index, state, set), result) in asked.iter().zip(results) {
        let answers = match result {
            Err(message) => {
                failures.push(format!("{}: {message}", file_name(set)));
                continue;
            }
            Ok(answers) => answers,
        };
        let rule_set = &scanned.rule_sets[*set];
        for (n, rule) in rule_set.rules.iter().enumerate() {
            let position = n + 1;
            let name = ask::question_name(position);
            let Some(answer) = answers.iter().find(|a| a.question == name) else {
                continue;
            };
            if answer.probability < rule_set.error_threshold {
                continue;
            }
            if !reported.insert((*state_index, rule.flags.clone())) {
                continue;
            }
            found.push(Finding {
                file: state.file.clone(),
                line_range: state.line_range,
                rule: format!("{}#{position}", file_name(set)),
                probability: (answer.probability * 100.0).round() / 100.0,
                flags: rule.flags.clone(),
                violation: rule.bad.join(" "),
                accepted: rule.good.join(" "),
                state: state.kind(),
            });
        }
    }
    let on_lines: HashSet<(String, String)> = found
        .iter()
        .filter(|f| f.state.extent() == Extent::Lines)
        .map(|f| (f.file.clone(), f.flags.clone()))
        .collect();
    found.retain(|f| {
        f.state.extent() == Extent::Lines || !on_lines.contains(&(f.file.clone(), f.flags.clone()))
    });
    Ok(Reviewed {
        findings: found,
        failures,
    })
}

use std::collections::BTreeMap;
use std::num::NonZeroUsize;
use std::path::Path;

use jevrev::files::{Files, InMemory};
use jevrev::git::{Diff, FileDiff, Hunk, Line};
use jevrev::languages;
use jevrev::lines::LineRange;
use jevrev::non_empty::NonEmpty;
use jevrev::rules::Language;
use jevrev::scan::{Changes, IgnoreRules, ScanScope, scan};
use jevrev::source_path::SourcePath;
use jevrev::state::State;

fn fixtures() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn language_name(language: Language) -> &'static str {
    match language {
        Language::Rust => "rust",
        Language::Python => "python",
        Language::Sh => "sh",
        Language::Just => "just",
        Language::Markdown => "markdown",
    }
}

#[test]
fn files_tile_into_the_captured_ranges() {
    for name in ["sample.py", "sample.rs", "script.sh"] {
        let directory = fixtures().join("states");
        let source = std::fs::read_to_string(directory.join(name)).unwrap();
        let expected: serde_json::Value =
            serde_json::from_slice(&std::fs::read(directory.join(format!("{name}.json"))).unwrap())
                .unwrap();
        let found: serde_json::Map<String, serde_json::Value> =
            languages::detect(&SourcePath::new(name, None).unwrap(), &source)
                .into_iter()
                .map(|language| {
                    let languages::Syntax::Valid(ranges) =
                        languages::ranges(language, source.as_bytes())
                    else {
                        panic!("{name} is not valid {}", language_name(language));
                    };
                    let ranges = ranges
                        .iter()
                        .map(|r| serde_json::json!([r.start(), r.end()]))
                        .collect();
                    (
                        language_name(language).to_string(),
                        serde_json::Value::Array(ranges),
                    )
                })
                .collect();
        assert_eq!(serde_json::Value::Object(found), expected, "{name}");
    }
}

fn record(state: &State) -> serde_json::Value {
    serde_json::json!({
        "file": state.file,
        "start": state.line_range.start(),
        "end": state.line_range.end(),
        "kind": state.kind(),
        "sets": state
            .sets
            .iter()
            .map(|set| set.file_name().unwrap().to_string_lossy().into_owned())
            .collect::<Vec<_>>(),
        "render": state.render(),
    })
}

fn line_of(line: &serde_json::Value) -> Line {
    let text = line["text"].as_str().unwrap().as_bytes().to_vec();
    match line["kind"].as_str().unwrap() {
        "added" => Line::Added(text),
        "context" => Line::Unchanged(text),
        "removed" => Line::Removed(text),
        kind => panic!("unknown hunk line kind {kind:?}"),
    }
}

fn side(start: usize, length: usize) -> Option<LineRange> {
    let length = NonZeroUsize::new(length)?;
    let start = NonZeroUsize::new(start)?;
    Some(LineRange::new(start, start.checked_add(length.get() - 1).unwrap()).unwrap())
}

fn diff_of(value: &serde_json::Value) -> Diff {
    let text = |v: &serde_json::Value| v.as_str().unwrap().to_string();
    let number = |v: &serde_json::Value| v.as_u64().unwrap() as usize;
    Diff {
        files: value
            .as_array()
            .unwrap()
            .iter()
            .map(|file| FileDiff {
                path: SourcePath::new(file["path"].as_str().unwrap(), None).unwrap(),
                hunks: NonEmpty::from_vec(
                    file["hunks"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|hunk| Hunk {
                            path: SourcePath::new(file["path"].as_str().unwrap(), None).unwrap(),
                            old: side(
                                number(&hunk["source_start"]),
                                number(&hunk["source_length"]),
                            ),
                            new: side(
                                number(&hunk["target_start"]),
                                number(&hunk["target_length"]),
                            ),
                            section: text(&hunk["section"]),
                            lines: NonEmpty::from_vec(
                                hunk["lines"]
                                    .as_array()
                                    .unwrap()
                                    .iter()
                                    .map(line_of)
                                    .collect(),
                            )
                            .expect("a hunk in the fixture has a line"),
                        })
                        .collect(),
                )
                .expect("a file diff in the fixture has a hunk"),
            })
            .collect(),
    }
}

#[tokio::test]
async fn changes_split_into_the_captured_states() {
    let directory = fixtures().join("change");
    let spec: serde_json::Value =
        serde_json::from_slice(&std::fs::read(directory.join("diff.json")).unwrap()).unwrap();
    let expected: serde_json::Value =
        serde_json::from_slice(&std::fs::read(directory.join("states.json")).unwrap()).unwrap();
    let files = Files::InMemory(InMemory::new(
        spec["files"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(path, text)| {
                (
                    SourcePath::new(path, None).unwrap(),
                    text.as_str().unwrap().as_bytes().to_vec(),
                )
            })
            .collect::<BTreeMap<_, _>>(),
    ));
    let changes = Changes {
        diff: diff_of(&spec["diff"]),
        untracked: spec["untracked"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| SourcePath::new(v.as_str().unwrap(), None).unwrap())
            .collect(),
    };
    let rules = tempfile::tempdir().unwrap();
    std::fs::write(
        rules.path().join("all.yaml"),
        "when:\n  - {}\nerror_threshold: 0.5\nrules:\n  - flags: x\n",
    )
    .unwrap();
    std::fs::write(
        rules.path().join("lang.yaml"),
        "when:\n  - language: [rust, python, sh]\nerror_threshold: 0.5\nrules:\n  - flags: x\n",
    )
    .unwrap();
    let scope = ScanScope {
        ignore: IgnoreRules::Off,
        include: Vec::new(),
        exclude: Vec::new(),
    };
    let scanned = scan(
        &[rules.path().to_path_buf()],
        &files,
        &scope,
        Some(&changes),
    )
    .await
    .unwrap();
    let found = serde_json::Value::Array(scanned.states.iter().map(record).collect());
    assert_eq!(found, expected);
}

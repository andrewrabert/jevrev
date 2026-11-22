use std::collections::BTreeMap;
use std::num::NonZeroUsize;

use anstyle::{AnsiColor, Color, Effects, Style};

use crate::jev::api::Probability;
use crate::state::StateKind;
use crate::store::{Finding, Review, Source, Verdict};
use crate::templates::{self, ReportRule, Template, by_rule};

const LOCATION: Style = Style::new().effects(Effects::BOLD);
const INDEX: Style = Style::new().effects(Effects::DIMMED);
const RULE: Style = Style::new().fg_color(Some(Color::Ansi(AnsiColor::Yellow)));
const STATE: Style = Style::new().fg_color(Some(Color::Ansi(AnsiColor::Cyan)));

fn probability_style(probability: Probability) -> Style {
    let probability = probability.into_inner();
    if probability >= 0.9 {
        Style::new()
            .effects(Effects::BOLD)
            .fg_color(Some(Color::Ansi(AnsiColor::Red)))
    } else if probability >= 0.7 {
        RULE
    } else {
        Style::new()
    }
}

pub fn findings_listing<'f>(findings: impl IntoIterator<Item = &'f Finding>) -> String {
    let mut sorted: Vec<&Finding> = findings.into_iter().collect();
    sorted.sort_by(|a, b| {
        (
            a.finding.file.as_str(),
            a.finding.line_range.start(),
            a.index,
        )
            .cmp(&(
                b.finding.file.as_str(),
                b.finding.line_range.start(),
                b.index,
            ))
    });
    let mut out = String::new();
    for f in sorted {
        let range = f.finding.line_range;
        let lines = if range.start() == range.end() {
            range.start().to_string()
        } else {
            format!("{}-{}", range.start(), range.end())
        };
        let p = probability_style(f.finding.probability);
        out.push_str(&format!(
            "{LOCATION}{}:{lines}{LOCATION:#} {INDEX}[{}]{INDEX:#} {RULE}{}{RULE:#} \
             {p}{:.2}{p:#} {STATE}{}{STATE:#}\n    {}\n",
            f.finding.file,
            f.index,
            f.finding.rule,
            f.finding.probability.into_inner(),
            f.finding.state.as_ref(),
            f.finding.flags
        ));
    }
    out
}

#[derive(serde::Serialize)]
struct PendingLine<'f> {
    accepted: &'f str,
    end: NonZeroUsize,
    file: &'f str,
    flags: &'f str,
    index: i64,
    probability: Probability,
    rule: &'f str,
    start: NonZeroUsize,
    state: StateKind,
    violation: &'f str,
}

pub fn pending_lines(review: &Review) -> String {
    let mut out = String::new();
    for f in &review.findings {
        if f.judgment.is_some() {
            continue;
        }
        let line = PendingLine {
            accepted: &f.finding.accepted,
            end: f.finding.line_range.end(),
            file: &f.finding.file,
            flags: &f.finding.flags,
            index: f.index,
            probability: f.finding.probability,
            rule: &f.finding.rule,
            start: f.finding.line_range.start(),
            state: f.finding.state,
            violation: &f.finding.violation,
        };
        out.push_str(&serde_json::to_string(&line).expect("a finding serializes"));
        out.push('\n');
    }
    out
}

pub fn review_json(review: &Review) -> String {
    serde_json::to_string(review).expect("a review serializes")
}

#[derive(serde::Serialize)]
struct FrontMatter<'r> {
    source: &'r Source,
    findings: usize,
    confirmed: usize,
    rejected: usize,
    #[serde(skip_serializing_if = "BTreeMap::is_empty")]
    metadata: &'r BTreeMap<String, String>,
    #[serde(skip_serializing_if = "<[String]>::is_empty")]
    include: &'r [String],
    #[serde(skip_serializing_if = "<[String]>::is_empty")]
    exclude: &'r [String],
}

fn front_matter(review: &Review) -> String {
    let front = FrontMatter {
        source: &review.source,
        findings: review.findings.len(),
        confirmed: review.count(Verdict::Confirmed),
        rejected: review.count(Verdict::Rejected),
        metadata: &review.metadata,
        include: &review.include,
        exclude: &review.exclude,
    };
    serde_saphyr::to_string(&front)
        .expect("the front matter is plain strings, numbers and maps")
        .trim_end_matches('\n')
        .to_string()
}

#[derive(serde::Serialize)]
struct ReviewText<'f> {
    front_matter: String,
    rules: Vec<ReportRule<'f>>,
}

pub fn review_markdown(review: &Review) -> String {
    let found = review.confirmed();
    templates::render(
        Template::Review,
        &ReviewText {
            front_matter: front_matter(review),
            rules: by_rule(&found),
        },
    )
}

use crate::git::{Diff, FileDiff};
use crate::lines::LineRange;
use crate::state::Extent;
use crate::store::Finding;
use crate::templates::{self, ReportRule, Template, by_rule};

const BODY_LIMIT: usize = 65536;

#[derive(serde::Serialize)]
struct PrComment<'f> {
    reason: &'f str,
    flags: &'f str,
}

#[derive(serde::Serialize)]
struct PrReviewBody<'f> {
    found: usize,
    outside: Option<Vec<ReportRule<'f>>>,
    left_out: usize,
}

fn file_diff<'d>(f: &Finding, diff: &'d Diff) -> Option<&'d FileDiff> {
    diff.files.iter().find(|file| file.path == f.finding.file)
}

pub fn in_diff(f: &Finding, diff: &Diff) -> bool {
    file_diff(f, diff).is_some()
}

pub fn commented_lines(f: &Finding, diff: &Diff) -> Option<LineRange> {
    if f.finding.state.extent() != Extent::Lines {
        return None;
    }
    file_diff(f, diff)?
        .hunks
        .iter()
        .filter_map(|hunk| hunk.new)
        .find_map(|new| f.finding.line_range.intersection(&new))
}

pub fn comment_body(f: &Finding) -> String {
    let reason = f.judgment.as_ref().map_or("", |j| j.reason.as_str());
    templates::render(
        Template::PrComment,
        &PrComment {
            reason,
            flags: &f.finding.flags,
        },
    )
}

fn render_body(found: usize, outside: Option<Vec<ReportRule<'_>>>, left_out: usize) -> String {
    templates::render(
        Template::PrReview,
        &PrReviewBody {
            found,
            outside,
            left_out,
        },
    )
}

pub fn review_body(found: usize, outside: &[&Finding]) -> String {
    if outside.is_empty() {
        return render_body(found, None, 0);
    }
    let size = |rules: Vec<ReportRule<'_>>, left_out: usize| {
        render_body(found, Some(rules), left_out).chars().count() as i64
    };
    let base = size(Vec::new(), 0);
    let mut used = base;
    let room = BODY_LIMIT as i64 - (size(Vec::new(), outside.len()) - base);
    let mut rules: Vec<ReportRule<'_>> = Vec::new();
    let mut left_out = 0;
    for group in by_rule(outside) {
        let flags = group.flags;
        let empty = size(
            vec![ReportRule {
                flags,
                findings: Vec::new(),
            }],
            0,
        );
        let heading = empty - base;
        let mut kept = Vec::new();
        for f in group.findings {
            let item = size(
                vec![ReportRule {
                    flags,
                    findings: vec![f],
                }],
                0,
            ) - empty;
            if used + heading + item > room {
                left_out += 1;
            } else {
                kept.push(f);
                used += item;
            }
        }
        if !kept.is_empty() {
            rules.push(ReportRule {
                flags,
                findings: kept,
            });
            used += heading;
        }
    }
    render_body(found, Some(rules), left_out)
}

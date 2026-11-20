use indexmap::IndexMap;

use crate::rules::{Rule, RuleSet};

pub const TASK: &str =
    "Judge only the lines that begin with \"+\" in this diff; other lines are context.";

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Instructions {
    pub task: String,
    pub flags: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
pub struct Criteria {
    #[serde(rename = "true")]
    pub when_true: String,
    #[serde(rename = "false")]
    pub when_false: String,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(tag = "type", rename = "noul")]
pub struct Noul {
    pub instructions: Instructions,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub criteria: Option<Criteria>,
}

pub fn question_name(position: usize) -> String {
    format!("q{position}")
}

fn noul(rule: &Rule) -> Noul {
    Noul {
        instructions: Instructions {
            task: TASK.into(),
            flags: rule.flags.clone(),
        },
        criteria: (!rule.good.is_empty() || !rule.bad.is_empty()).then(|| Criteria {
            when_true: rule.bad.join(" "),
            when_false: rule.good.join(" "),
        }),
    }
}

pub fn questions(rule_set: &RuleSet) -> IndexMap<String, Noul> {
    rule_set
        .rules
        .iter()
        .enumerate()
        .map(|(n, rule)| (question_name(n + 1), noul(rule)))
        .collect()
}

use crate::Error;
use crate::jev::api::{Question, Questions};
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

pub fn question_name(position: usize) -> String {
    format!("q{position}")
}

fn noul(rule: &Rule) -> Question {
    Question::Noul {
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

pub fn questions(rule_set: &RuleSet) -> Result<Questions, Error> {
    Questions::try_new(
        rule_set
            .rules
            .iter()
            .enumerate()
            .map(|(n, rule)| (question_name(n + 1), noul(rule)))
            .collect(),
    )
}

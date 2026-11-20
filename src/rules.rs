use crate::Error;
use crate::gitignore::Pattern;
use anyhow::{anyhow, bail};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Language {
    Rust,
    Python,
    Sh,
    Just,
    Markdown,
}

#[derive(Debug, Clone, serde::Deserialize)]
#[serde(try_from = "RawWhen")]
pub struct When {
    pub language: Vec<Language>,
    pub matches: Vec<Pattern>,
    pub exists: Vec<Pattern>,
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    pub flags: String,
    #[serde(default)]
    pub good: Vec<String>,
    #[serde(default)]
    pub bad: Vec<String>,
}

#[derive(Debug, Clone)]
pub enum Conditions {
    Given(Vec<When>),
    Absent,
}

#[derive(Debug, Clone)]
pub struct RuleSet {
    pub when: Conditions,
    pub error_threshold: f64,
    pub rules: Vec<Rule>,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RawWhen {
    #[serde(default)]
    language: Vec<Language>,
    #[serde(default)]
    matches: Vec<String>,
    #[serde(default)]
    exists: Vec<String>,
}

impl TryFrom<RawWhen> for When {
    type Error = Error;

    fn try_from(raw: RawWhen) -> Result<When, Error> {
        let patterns = |texts: &[String]| texts.iter().map(|text| Pattern::new(text)).collect();
        Ok(When {
            language: raw.language,
            matches: patterns(&raw.matches),
            exists: patterns(&raw.exists),
        })
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct RawRuleSet {
    #[serde(rename = "description")]
    _description: Option<String>,
    when: Option<Vec<When>>,
    error_threshold: f64,
    rules: Vec<Rule>,
}

impl RuleSet {
    pub fn parse(text: &str) -> Result<RuleSet, Error> {
        let raw: RawRuleSet = serde_saphyr::from_str(text)?;
        if !(0.0..=1.0).contains(&raw.error_threshold) {
            bail!(
                "error_threshold: {} is not between 0 and 1",
                raw.error_threshold
            );
        }
        Ok(RuleSet {
            when: raw.when.map_or(Conditions::Absent, Conditions::Given),
            error_threshold: raw.error_threshold,
            rules: raw.rules,
        })
    }

    pub fn load(path: &std::path::Path) -> Result<RuleSet, Error> {
        let text = std::fs::read(path)
            .map_err(|e| e.to_string())
            .and_then(|bytes| String::from_utf8(bytes).map_err(|e| e.to_string()))
            .map_err(|cause| anyhow!("rule set {}: {cause}", path.display()))?;
        RuleSet::parse(&text).map_err(|cause| anyhow!("rule set {}: {cause}", path.display()))
    }
}

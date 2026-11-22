use anyhow::{Context, anyhow};
use std::path::Path;

use std::fs::File;
use std::io::{Read, Seek, Write};

use crate::git::{GitDiff, GitRev, LocalSelection};
use crate::github::pull_request::GithubPullRequest;
use crate::github::repo::GithubRepo;
use crate::{Error, scan};
use validator::Validate;

#[derive(
    Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize, strum::Display,
)]
#[serde(rename_all = "lowercase")]
#[strum(serialize_all = "lowercase")]
pub enum Verdict {
    Confirmed,
    Rejected,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Judgment {
    pub verdict: Verdict,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize, validator::Validate)]
pub struct Finding {
    #[serde(flatten)]
    pub finding: scan::Finding,
    #[validate(range(min = 1))]
    pub index: i64,
    #[serde(flatten, with = "judgment_fields")]
    pub judgment: Option<Judgment>,
}

mod judgment_fields {
    use super::{Judgment, Verdict};
    use serde::{Deserialize, Deserializer, Serialize, Serializer};

    #[derive(Serialize, Deserialize)]
    struct Fields {
        verdict: Option<Verdict>,
        reason: Option<String>,
    }

    pub fn serialize<S: Serializer>(
        judgment: &Option<Judgment>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        let (verdict, reason) = match judgment {
            Some(j) => (Some(j.verdict), Some(j.reason.clone())),
            None => (None, None),
        };
        Fields { verdict, reason }.serialize(serializer)
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Judgment>, D::Error> {
        match Fields::deserialize(deserializer)? {
            Fields {
                verdict: Some(verdict),
                reason: Some(reason),
            } => Ok(Some(Judgment { verdict, reason })),
            Fields {
                verdict: None,
                reason: None,
            } => Ok(None),
            _ => Err(serde::de::Error::custom(
                "verdict and reason are both null or both given",
            )),
        }
    }
}

impl Finding {
    pub fn verdict(&self) -> Option<Verdict> {
        self.judgment.as_ref().map(|j| j.verdict)
    }

    pub fn new(finding: scan::Finding, index: i64) -> Finding {
        Finding {
            finding,
            index,
            judgment: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(untagged)]
pub enum GithubSelection {
    PullRequest(GithubPullRequest),
    Diff(GitDiff),
    Rev(GitRev),
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(untagged)]
pub enum Source {
    GithubRepo {
        #[serde(flatten)]
        repo: GithubRepo,
        git: GithubSelection,
    },
    LocalGitDir {
        path: String,
        git: LocalSelection,
    },
    LocalDir {
        path: String,
    },
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct Review {
    pub source: Source,
    pub include: Vec<String>,
    pub exclude: Vec<String>,
    pub findings: Vec<Finding>,
}

impl Review {
    pub fn confirmed(&self) -> Vec<&Finding> {
        self.findings
            .iter()
            .filter(|f| f.verdict() == Some(Verdict::Confirmed))
            .collect()
    }

    pub fn count(&self, verdict: Verdict) -> usize {
        self.findings
            .iter()
            .filter(|f| f.verdict() == Some(verdict))
            .count()
    }
}

#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, PartialEq)]
pub enum Stored {
    Review(Review),
    Empty,
}

pub struct Store {
    file: File,
}

impl Store {
    pub fn load(&self) -> Result<Stored, Error> {
        let mut text = String::new();
        (&self.file)
            .rewind()
            .and_then(|()| (&self.file).read_to_string(&mut text))
            .context("review file")?;
        if text.is_empty() {
            return Ok(Stored::Empty);
        }
        let review: Review = serde_json::from_str(&text).context("review file")?;
        for finding in &review.findings {
            finding
                .finding
                .validate()
                .and_then(|()| finding.validate())
                .map_err(|e| anyhow!("finding: {e}"))?;
        }
        Ok(Stored::Review(review))
    }

    pub fn save(&self, review: &Review) -> Result<(), Error> {
        let text = serde_json::to_string(review).expect("a review serializes");
        let mut file = &self.file;
        file.set_len(0)
            .and_then(|()| file.rewind())
            .and_then(|()| file.write_all(text.as_bytes()))
            .and_then(|()| file.sync_all())
            .context("review file")
    }
}

pub fn open<T>(path: &Path, run: impl FnOnce(&Store) -> Result<T, Error>) -> Result<T, Error> {
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .with_context(|| format!("{}", path.display()))?;
    file.try_lock().map_err(|e| match e {
        std::fs::TryLockError::WouldBlock => anyhow!("the review file is locked"),
        std::fs::TryLockError::Error(e) => anyhow!("{}: {e}", path.display()),
    })?;
    run(&Store { file })
}

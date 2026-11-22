use anyhow::{Context, anyhow};
use std::path::Path;

use redb::{Database, ReadableDatabase, ReadableTable, ReadableTableMetadata, TableDefinition};

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

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Judgment {
    pub verdict: Verdict,
    pub reason: String,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize, validator::Validate)]
pub struct Finding {
    pub finding: scan::Finding,
    #[validate(range(min = 1))]
    pub index: i64,
    pub judgment: Option<Judgment>,
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
pub enum GithubSelection {
    PullRequest(GithubPullRequest),
    Diff(GitDiff),
    Rev(GitRev),
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum Source {
    GithubRepo {
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

#[derive(Debug, serde::Serialize, serde::Deserialize)]
struct Header {
    source: Source,
    include: Vec<String>,
    exclude: Vec<String>,
}

macro_rules! postcard_value {
    ($type:ty, $name:literal) => {
        impl redb::Value for $type {
            type SelfType<'a> = $type;
            type AsBytes<'a> = Vec<u8>;

            fn fixed_width() -> Option<usize> {
                None
            }

            fn from_bytes<'a>(data: &'a [u8]) -> $type
            where
                Self: 'a,
            {
                postcard::from_bytes(data).expect(concat!("a stored ", $name, " decodes"))
            }

            fn as_bytes<'a, 'b: 'a>(value: &'a $type) -> Vec<u8>
            where
                Self: 'b,
            {
                postcard::to_stdvec(value).expect(concat!("a ", $name, " encodes"))
            }

            fn type_name() -> redb::TypeName {
                redb::TypeName::new(concat!("jevrev::", $name))
            }
        }
    };
}

postcard_value!(Header, "Header");
postcard_value!(scan::Finding, "Finding");
postcard_value!(Judgment, "Judgment");

const REVIEW: TableDefinition<(), Header> = TableDefinition::new("review");
const FINDINGS: TableDefinition<u64, scan::Finding> = TableDefinition::new("findings");
const VERDICTS: TableDefinition<u64, Judgment> = TableDefinition::new("verdicts");

pub struct Store {
    db: Database,
}

impl Store {
    pub fn load(&self) -> Result<Stored, Error> {
        self.read().context("review file")
    }

    fn read(&self) -> Result<Stored, Error> {
        let read = self.db.begin_read()?;
        let review = match read.open_table(REVIEW) {
            Ok(table) => table,
            Err(redb::TableError::TableDoesNotExist(_)) => return Ok(Stored::Empty),
            Err(e) => return Err(e.into()),
        };
        let Header {
            source,
            include,
            exclude,
        } = review
            .get(())?
            .ok_or_else(|| anyhow!("the review table has no header"))?
            .value();
        let verdicts = read.open_table(VERDICTS)?;
        let mut findings = Vec::new();
        for (position, entry) in read.open_table(FINDINGS)?.iter()?.enumerate() {
            let (index, finding) = entry?;
            let index = index.value();
            if index != position as u64 + 1 {
                return Err(anyhow!("finding {index} follows finding {position}"));
            }
            let finding = Finding {
                finding: finding.value(),
                index: i64::try_from(index)?,
                judgment: verdicts.get(index)?.map(|judgment| judgment.value()),
            };
            finding
                .finding
                .validate()
                .and_then(|()| finding.validate())
                .map_err(|e| anyhow!("finding: {e}"))?;
            findings.push(finding);
        }
        if let Some(entry) = verdicts.iter()?.next_back() {
            let index = entry?.0.value();
            if index > findings.len() as u64 {
                return Err(anyhow!("verdict for finding {index}, which does not exist"));
            }
        }
        Ok(Stored::Review(Review {
            source,
            include,
            exclude,
            findings,
        }))
    }

    /// Replaces the stored review with `review`.
    pub fn save(&self, review: &Review) -> Result<(), Error> {
        self.write(review).context("review file")
    }

    fn write(&self, review: &Review) -> Result<(), Error> {
        let header = Header {
            source: review.source.clone(),
            include: review.include.clone(),
            exclude: review.exclude.clone(),
        };
        let write = self.db.begin_write()?;
        write.delete_table(REVIEW)?;
        write.delete_table(FINDINGS)?;
        write.delete_table(VERDICTS)?;
        write.open_table(REVIEW)?.insert((), &header)?;
        {
            let mut findings = write.open_table(FINDINGS)?;
            let mut verdicts = write.open_table(VERDICTS)?;
            for finding in &review.findings {
                let index = u64::try_from(finding.index)?;
                findings.insert(index, &finding.finding)?;
                if let Some(judgment) = &finding.judgment {
                    verdicts.insert(index, judgment)?;
                }
            }
        }
        write.commit()?;
        Ok(())
    }

    /// Records `judgment` as the verdict of finding `index`.
    pub fn judge(&self, index: u64, judgment: &Judgment) -> Result<(), JudgeError> {
        let write = self.db.begin_write().map_err(stored)?;
        if write
            .open_table(REVIEW)
            .map_err(stored)?
            .get(())
            .map_err(stored)?
            .is_none()
        {
            return Err(JudgeError::NoReview);
        }
        {
            let findings = write.open_table(FINDINGS).map_err(stored)?;
            if findings.get(index).map_err(stored)?.is_none() {
                let count = findings.len().map_err(stored)?;
                return Err(JudgeError::NoFinding { index, count });
            }
            let mut verdicts = write.open_table(VERDICTS).map_err(stored)?;
            if let Some(judged) = verdicts.get(index).map_err(stored)? {
                return Err(JudgeError::Judged {
                    index,
                    verdict: judged.value().verdict,
                });
            }
            verdicts.insert(index, judgment).map_err(stored)?;
        }
        write.commit().map_err(stored)
    }
}

#[derive(Debug, thiserror::Error)]
pub enum JudgeError {
    #[error("The review file holds no review.")]
    NoReview,
    #[error("index: {index} is not in 1..{count}")]
    NoFinding { index: u64, count: u64 },
    #[error("index: finding {index} is already {verdict}")]
    Judged { index: u64, verdict: Verdict },
    #[error("review file: {0:#}")]
    Store(Error),
}

fn stored(error: impl Into<Error>) -> JudgeError {
    JudgeError::Store(error.into())
}

pub fn open<T>(path: &Path, run: impl FnOnce(&Store) -> Result<T, Error>) -> Result<T, Error> {
    let db = Database::create(path).map_err(|e| match e {
        redb::DatabaseError::DatabaseAlreadyOpen => anyhow!("the review file is locked"),
        e => anyhow!("{}: {e}", path.display()),
    })?;
    run(&Store { db })
}

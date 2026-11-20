use anyhow::{anyhow, bail};
use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::num::{NonZeroU64, NonZeroUsize};
use std::sync::Mutex;
use std::time::Duration;

use bytes::Bytes;
use futures::{StreamExt, TryStreamExt};
use http_body_util::BodyExt;
use http_body_util::combinators::BoxBody;
use octocrab::Octocrab;

use crate::Error;
use crate::files::InMemory;
use crate::git::{self, ContentPair, Diff, GitObjectId, GitRevision, Side};
use crate::github::credentials::Token;
use crate::github::pull_request::{GithubPullRequest, PullRequest, User};
use crate::github::repo::{DEFAULT_HOST, GithubHost, GithubRepo};
use crate::github::review;
use crate::store::{Finding, Review};

const DEFAULT_API: &str = "https://api.github.com";

const ADD_THREAD: &str = "
mutation(
  $review: ID!, $path: String!, $body: String!, $subjectType: PullRequestReviewThreadSubjectType!,
  $line: Int, $startLine: Int, $side: DiffSide
) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, body: $body, subjectType: $subjectType,
    line: $line, startLine: $startLine, side: $side
  }) { thread { id } }
}
";

const BLOB_CONCURRENCY: usize = 8;

const TIMEOUT: Duration = Duration::from_secs(300);

const FILE_MODES: [&str; 2] = ["100644", "100755"];

const REVIEW_DIFF_CONTEXT: u32 = 3;
const REVIEW_INTER_HUNK_CONTEXT: u32 = 1;

#[derive(Debug, thiserror::Error)]
pub enum SubmitError {
    #[error("the review has no confirmed findings")]
    NoConfirmedFindings,
    #[error(transparent)]
    Github(#[from] Error),
}

#[derive(Clone)]
pub struct Client {
    rest: Octocrab,
    graphql: Octocrab,
    rest_base: url::Url,
}

pub struct Blobs {
    client: Client,
    repo: GithubRepo,
    blobs: BTreeMap<String, GitObjectId>,
    read: Mutex<HashMap<String, Vec<u8>>>,
}

impl Blobs {
    pub fn paths(&self) -> Vec<String> {
        self.blobs.keys().cloned().collect()
    }

    pub async fn read(&self, path: &str) -> Result<Vec<u8>, Error> {
        if let Some(found) = self
            .read
            .lock()
            .ok()
            .and_then(|read| read.get(path).cloned())
        {
            return Ok(found);
        }
        let sha = self
            .blobs
            .get(path)
            .ok_or_else(|| anyhow!("{path}: no such file"))?;
        let contents = self.client.blob(&self.repo, sha).await?;
        if let Ok(mut read) = self.read.lock() {
            read.insert(path.to_string(), contents.clone());
        }
        Ok(contents)
    }
}

fn failed(what: &str, error: impl std::fmt::Display + std::fmt::Debug) -> Error {
    tracing::error!("{what}: {error:?}");
    anyhow!("{what}: {error}")
}

fn sha(what: &str, text: &str) -> Result<GitObjectId, Error> {
    GitObjectId::parse(text).map_err(|e| failed(what, e))
}

async fn bytes(
    what: &str,
    response: octocrab::Result<http::Response<BoxBody<Bytes, octocrab::Error>>>,
) -> Result<Vec<u8>, Error> {
    let response = octocrab::map_github_error(response.map_err(|e| failed(what, e))?)
        .await
        .map_err(|e| failed(what, e))?;
    let body = response
        .into_body()
        .collect()
        .await
        .map_err(|e| failed(what, e))?;
    Ok(body.to_bytes().to_vec())
}

#[derive(serde::Deserialize)]
struct PendingReview {
    id: u64,
    node_id: String,
}

#[derive(serde::Deserialize)]
struct SubmittedReview {
    html_url: url::Url,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
enum ReviewEvent {
    Comment,
    RequestChanges,
}

#[derive(serde::Serialize)]
struct StartReviewRequest<'r> {
    commit_id: &'r GitObjectId,
    body: String,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
enum ReviewSide {
    Right,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
enum ThreadSubject {
    Line,
    File,
}

#[derive(serde::Serialize)]
struct AddThreadRequest<'r> {
    query: &'static str,
    variables: AddThreadVariables<'r>,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct AddThreadVariables<'r> {
    review: &'r str,
    path: &'r str,
    body: String,
    subject_type: ThreadSubject,
    line: Option<NonZeroUsize>,
    start_line: Option<NonZeroUsize>,
    side: Option<ReviewSide>,
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
struct AddThreadResponse {
    add_pull_request_review_thread: AddedReviewThread,
}

#[derive(serde::Deserialize)]
struct AddedReviewThread {
    thread: ReviewThread,
}

#[derive(serde::Deserialize)]
struct ReviewThread {
    id: String,
}

#[derive(serde::Serialize)]
struct SubmitReviewRequest {
    event: ReviewEvent,
}

fn with_segments(url: &url::Url, segments: &[&str]) -> url::Url {
    let mut url = url.clone();
    url.path_segments_mut()
        .expect("an https URL has path segments")
        .pop_if_empty()
        .extend(segments);
    url
}

impl Client {
    pub fn new(host: &GithubHost, token: &Token) -> Result<Client, Error> {
        let (rest, graphql) = if host.as_str() == DEFAULT_HOST {
            let api = url::Url::parse(DEFAULT_API).expect("a valid URL");
            (api.clone(), api)
        } else {
            (
                with_segments(host.url(), &["api", "v3"]),
                with_segments(host.url(), &["api"]),
            )
        };
        let build = |base: &url::Url| {
            Octocrab::builder()
                .personal_token(token.as_str().to_string())
                .base_uri(base.as_str())
                .and_then(|builder| {
                    builder
                        .set_connect_timeout(Some(TIMEOUT))
                        .set_read_timeout(Some(TIMEOUT))
                        .build()
                })
                .map_err(|e| failed("GitHub client", e))
        };
        Ok(Client {
            rest: build(&rest)?,
            graphql: build(&graphql)?,
            rest_base: rest,
        })
    }

    fn route(&self, repo: &GithubRepo, segments: &[&str]) -> url::Url {
        let mut url = with_segments(
            &self.rest_base,
            &[
                "repos",
                repo.name().org().as_str(),
                repo.name().name().as_str(),
            ],
        );
        url.path_segments_mut()
            .expect("an https URL has path segments")
            .extend(segments);
        url
    }

    pub async fn current_user(&self) -> Result<User, Error> {
        let user = self
            .rest
            .current()
            .user()
            .await
            .map_err(|e| failed("GET /user", e))?;
        Ok(User { login: user.login })
    }

    pub async fn pull_request(
        &self,
        repo: &GithubRepo,
        number: NonZeroU64,
    ) -> Result<PullRequest, Error> {
        let what = format!("pull request {number}");
        let pr = self
            .rest
            .pulls(repo.name().org().as_str(), repo.name().name().as_str())
            .get(number.get())
            .await
            .map_err(|e| failed(&what, e))?;
        let author = pr.user.ok_or_else(|| failed(&what, "it has no author"))?;
        Ok(PullRequest {
            author: User {
                login: author.login,
            },
            head: sha(&what, &pr.head.sha)?,
            target: sha(&what, &pr.base.sha)?,
        })
    }

    pub async fn commit(&self, repo: &GithubRepo, rev: &GitRevision) -> Result<GitObjectId, Error> {
        let what = format!("commit {rev}");
        let commit = self
            .rest
            .commits(repo.name().org().as_str(), repo.name().name().as_str())
            .get(rev.as_str())
            .await
            .map_err(|e| failed(&what, e))?;
        sha(&what, &commit.sha)
    }

    pub async fn default_branch_head(&self, repo: &GithubRepo) -> Result<GitObjectId, Error> {
        let what = "the default branch";
        let found = self
            .rest
            .repos(repo.name().org().as_str(), repo.name().name().as_str())
            .get()
            .await
            .map_err(|e| failed(what, e))?;
        let branch = found
            .default_branch
            .ok_or_else(|| failed(what, "the repository has none"))?;
        let branch = GitRevision::parse(&branch).map_err(|e| failed(what, e))?;
        self.commit(repo, &branch).await
    }

    pub async fn merge_base(
        &self,
        repo: &GithubRepo,
        first: &GitObjectId,
        second: &GitObjectId,
    ) -> Result<GitObjectId, Error> {
        let what = format!("merge base of {first} and {second}");
        let compared = self
            .rest
            .commits(repo.name().org().as_str(), repo.name().name().as_str())
            .compare(first.as_str(), second.as_str())
            .send()
            .await
            .map_err(|e| failed(&what, e))?;
        sha(&what, &compared.merge_base_commit.sha)
    }

    pub async fn tarball(
        &self,
        repo: &GithubRepo,
        commit: &GitObjectId,
    ) -> Result<InMemory, Error> {
        let what = format!("tarball at {commit}");
        let response = self
            .rest
            .repos(repo.name().org().as_str(), repo.name().name().as_str())
            .download_tarball(commit.as_str())
            .await;
        let archive = bytes(&what, response).await?;
        let broken = |e: std::io::Error| failed(&what, e);
        let mut contents = BTreeMap::new();
        let mut tar = tar::Archive::new(flate2::read::GzDecoder::new(archive.as_slice()));
        for entry in tar.entries().map_err(broken)? {
            let mut entry = entry.map_err(broken)?;
            let regular = matches!(
                entry.header().entry_type(),
                tar::EntryType::Regular | tar::EntryType::Continuous | tar::EntryType::GNUSparse
            );
            if !regular {
                continue;
            }
            let Ok(name) = String::from_utf8(entry.path_bytes().into_owned()) else {
                continue;
            };
            let path = name
                .split_once('/')
                .map_or("", |(_, rest)| rest)
                .to_string();
            if path.is_empty() {
                continue;
            }
            let mut data = Vec::new();
            entry.read_to_end(&mut data).map_err(broken)?;
            contents.insert(path, data);
        }
        Ok(InMemory::new(contents))
    }

    async fn blob_shas(
        &self,
        repo: &GithubRepo,
        commit: &GitObjectId,
    ) -> Result<BTreeMap<String, GitObjectId>, Error> {
        let what = format!("tree at {commit}");
        let tree = self
            .rest
            .git(repo.name().org().as_str(), repo.name().name().as_str())
            .get_tree(commit.as_str())
            .recursive(true)
            .send()
            .await
            .map_err(|e| failed(&what, e))?;
        if tree.truncated {
            bail!(
                "{} has more files at {commit} than GitHub lists in one tree",
                repo.name()
            );
        }
        tree.tree
            .into_iter()
            .filter(|entry| entry.r#type == "blob" && FILE_MODES.contains(&entry.mode.as_str()))
            .map(|entry| Ok((entry.path, sha(&what, &entry.sha)?)))
            .collect()
    }

    pub async fn tree(&self, repo: &GithubRepo, commit: &GitObjectId) -> Result<Blobs, Error> {
        Ok(Blobs {
            client: self.clone(),
            repo: repo.clone(),
            blobs: self.blob_shas(repo, commit).await?,
            read: Mutex::new(HashMap::new()),
        })
    }

    pub async fn blob(&self, repo: &GithubRepo, sha: &GitObjectId) -> Result<Vec<u8>, Error> {
        let mut headers = http::HeaderMap::new();
        headers.insert(
            http::header::ACCEPT,
            http::HeaderValue::from_static("application/vnd.github.raw+json"),
        );
        let route = self.route(repo, &["git", "blobs", sha.as_str()]);
        let response = self
            .rest
            ._get_with_headers(route.as_str(), Some(headers))
            .await;
        bytes(&format!("blob {sha}"), response).await
    }

    pub async fn diff(
        &self,
        repo: &GithubRepo,
        base: &GitObjectId,
        head: &GitObjectId,
        context: u32,
        inter_hunk_context: u32,
    ) -> Result<Diff, Error> {
        let old = self.blob_shas(repo, base).await?;
        let new = self.blob_shas(repo, head).await?;
        let changed: Vec<(String, Option<GitObjectId>, GitObjectId)> = new
            .iter()
            .filter(|(path, sha)| old.get(*path) != Some(*sha))
            .map(|(path, sha)| (path.clone(), old.get(path).cloned(), sha.clone()))
            .collect();
        let read = |sha: Option<GitObjectId>| async move {
            match sha {
                None => Ok(Side::Absent),
                Some(sha) => self.blob(repo, &sha).await.map(Side::Present),
            }
        };
        let pairs: Vec<ContentPair> = futures::stream::iter(changed)
            .map(|(path, before, after)| async move {
                let (old, new) =
                    futures::future::try_join(read(before), self.blob(repo, &after)).await?;
                Ok::<_, Error>(ContentPair { path, old, new })
            })
            .buffered(BLOB_CONCURRENCY / 2)
            .try_collect()
            .await?;
        Ok(git::diff_contents(&pairs, context, inter_hunk_context))
    }

    pub async fn submit_review(
        &self,
        repo: &GithubRepo,
        pr: &GithubPullRequest,
        review: &Review,
    ) -> Result<url::Url, SubmitError> {
        if review.confirmed().is_empty() {
            return Err(SubmitError::NoConfirmedFindings);
        }
        let pull_request = self.pull_request(repo, pr.number).await?;
        let reviewer = self.current_user().await?;
        let diff = self
            .diff(
                repo,
                &pr.base,
                &pr.head,
                REVIEW_DIFF_CONTEXT,
                REVIEW_INTER_HUNK_CONTEXT,
            )
            .await?;
        let reviews = self.route(repo, &["pulls", &pr.number.to_string(), "reviews"]);
        let pending = self.start_review(&reviews, pr, review, &diff).await?;
        let finished = async {
            self.add_threads(&pending, review, &diff).await?;
            self.finish_review(&reviews, &pending, &pull_request.author, &reviewer)
                .await
        }
        .await;
        match finished {
            Ok(url) => Ok(url),
            Err(error) => {
                if let Err(e) = self
                    .rest
                    ._delete(
                        with_segments(&reviews, &[&pending.id.to_string()]).as_str(),
                        None::<&()>,
                    )
                    .await
                {
                    tracing::warn!(
                        "could not delete pending review {} after the failed submit: {e}",
                        pending.id
                    );
                }
                Err(error.into())
            }
        }
    }

    async fn start_review(
        &self,
        reviews: &url::Url,
        pr: &GithubPullRequest,
        review: &Review,
        diff: &Diff,
    ) -> Result<PendingReview, Error> {
        let confirmed = review.confirmed();
        let outside: Vec<&Finding> = confirmed
            .iter()
            .copied()
            .filter(|f| !review::in_diff(f, diff))
            .collect();
        self.rest
            .post(
                reviews.as_str(),
                Some(&StartReviewRequest {
                    commit_id: &pr.head,
                    body: review::review_body(confirmed.len(), &outside),
                }),
            )
            .await
            .map_err(|e| failed("starting the review", e))
    }

    async fn add_threads(
        &self,
        pending: &PendingReview,
        review: &Review,
        diff: &Diff,
    ) -> Result<(), Error> {
        for f in review.confirmed() {
            if !review::in_diff(f, diff) {
                continue;
            }
            let variables = match review::commented_lines(f, diff) {
                Some(lines) => AddThreadVariables {
                    review: &pending.node_id,
                    path: &f.finding.file,
                    body: review::comment_body(f),
                    subject_type: ThreadSubject::Line,
                    line: Some(lines.end()),
                    start_line: (lines.start() != lines.end()).then(|| lines.start()),
                    side: Some(ReviewSide::Right),
                },
                None => AddThreadVariables {
                    review: &pending.node_id,
                    path: &f.finding.file,
                    body: review::comment_body(f),
                    subject_type: ThreadSubject::File,
                    line: None,
                    start_line: None,
                    side: None,
                },
            };
            let added: AddThreadResponse = self
                .graphql
                .graphql(&AddThreadRequest {
                    query: ADD_THREAD,
                    variables,
                })
                .await
                .map_err(|e| failed("adding a comment", e))?;
            if added.add_pull_request_review_thread.thread.id.is_empty() {
                return Err(failed("adding a comment", "no thread was added"));
            }
        }
        Ok(())
    }

    async fn finish_review(
        &self,
        reviews: &url::Url,
        pending: &PendingReview,
        author: &User,
        reviewer: &User,
    ) -> Result<url::Url, Error> {
        // GitHub refuses a request for changes on one's own pull request.
        let event = if author.login == reviewer.login {
            ReviewEvent::Comment
        } else {
            ReviewEvent::RequestChanges
        };
        let submitted: SubmittedReview = self
            .rest
            .post(
                with_segments(reviews, &[&pending.id.to_string(), "events"]).as_str(),
                Some(&SubmitReviewRequest { event }),
            )
            .await
            .map_err(|e| failed("submitting the review", e))?;
        Ok(submitted.html_url)
    }
}

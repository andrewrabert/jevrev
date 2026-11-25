use anyhow::anyhow;
use std::num::{NonZeroU64, NonZeroUsize};
use std::time::Duration;

use octocrab::Octocrab;

use crate::Error;
use crate::git::{Diff, GitObjectId, GitRevision};
use crate::github::credentials::Token;
use crate::github::pull_request::{GithubPullRequest, PullRequest, User};
use crate::github::repo::{GithubHost, GithubRepo};
use crate::github::review;
use crate::store::{Finding, Review};

const DEFAULT_HOST: &str = "github.com";
const DEFAULT_API: &str = "https://api.github.com";

const ADD_FILE_THREAD: &str = "
mutation($review: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, body: $body, subjectType: FILE
  }) { thread { id } }
}
";

const TIMEOUT: Duration = Duration::from_secs(300);

pub const REVIEW_DIFF_CONTEXT: u32 = 3;
pub const REVIEW_INTER_HUNK_CONTEXT: u32 = 1;

#[derive(Debug, thiserror::Error)]
pub enum SubmitError {
    #[error("the review has no confirmed findings")]
    NoConfirmedFindings,
    #[error(transparent)]
    Github(#[from] Error),
}

#[derive(Clone)]
pub struct Client {
    host: GithubHost,
    rest: Octocrab,
    graphql: Octocrab,
    rest_base: url::Url,
}

fn failed(what: &str, error: impl std::fmt::Display + std::fmt::Debug) -> Error {
    tracing::error!("{what}: {error:?}");
    anyhow!("{what}: {error}")
}

fn sha(what: &str, text: &str) -> Result<GitObjectId, Error> {
    GitObjectId::parse(text).map_err(|e| failed(what, e))
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
    comments: Vec<LineComment<'r>>,
}

#[derive(serde::Serialize)]
struct LineComment<'r> {
    path: &'r str,
    body: String,
    line: NonZeroUsize,
    #[serde(skip_serializing_if = "Option::is_none")]
    start_line: Option<NonZeroUsize>,
    side: ReviewSide,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
enum ReviewSide {
    Right,
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
    pub fn new(host: Option<GithubHost>, token: &Token) -> Result<Client, Error> {
        let host = match host {
            Some(host) => host,
            None => GithubHost::try_new(DEFAULT_HOST)?,
        };
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
            host,
            rest: build(&rest)?,
            graphql: build(&graphql)?,
            rest_base: rest,
        })
    }

    pub fn host(&self) -> &GithubHost {
        &self.host
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

    pub async fn submit_review(
        &self,
        repo: &GithubRepo,
        pr: &GithubPullRequest,
        review: &Review,
        diff: &Diff,
    ) -> Result<url::Url, SubmitError> {
        if review.confirmed().is_empty() {
            return Err(SubmitError::NoConfirmedFindings);
        }
        let pull_request = self.pull_request(repo, pr.number).await?;
        let reviewer = self.current_user().await?;
        let reviews = self.route(repo, &["pulls", &pr.number.to_string(), "reviews"]);
        let pending = self.start_review(&reviews, pr, review, diff).await?;
        let finished = async {
            self.add_file_threads(&pending, review, diff).await?;
            self.finish_review(&reviews, &pending, &pull_request.author, &reviewer)
                .await
        }
        .await;
        match finished {
            Ok(url) => Ok(url),
            Err(error) => {
                match self
                    .rest
                    ._delete(
                        with_segments(&reviews, &[&pending.id.to_string()]).as_str(),
                        None::<&()>,
                    )
                    .await
                {
                    Ok(_) => Err(error.into()),
                    Err(e) => Err(error
                        .context(format!(
                            "the submit failed, and deleting its pending review {} failed too: {e}",
                            pending.id
                        ))
                        .into()),
                }
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
        let comments = confirmed
            .iter()
            .filter_map(|f| {
                let lines = review::commented_lines(f, diff)?;
                Some(LineComment {
                    path: &f.finding.file,
                    body: review::comment_body(f),
                    line: lines.end(),
                    start_line: (lines.start() != lines.end()).then(|| lines.start()),
                    side: ReviewSide::Right,
                })
            })
            .collect();
        self.rest
            .post(
                reviews.as_str(),
                Some(&StartReviewRequest {
                    commit_id: &pr.head,
                    body: review::review_body(confirmed.len(), &outside),
                    comments,
                }),
            )
            .await
            .map_err(|e| failed("starting the review", e))
    }

    async fn add_file_threads(
        &self,
        pending: &PendingReview,
        review: &Review,
        diff: &Diff,
    ) -> Result<(), Error> {
        for f in review.confirmed() {
            if !review::in_diff(f, diff) || review::commented_lines(f, diff).is_some() {
                continue;
            }
            let variables = AddThreadVariables {
                review: &pending.node_id,
                path: &f.finding.file,
                body: review::comment_body(f),
            };
            let added: AddThreadResponse = self
                .graphql
                .graphql(&AddThreadRequest {
                    query: ADD_FILE_THREAD,
                    variables,
                })
                .await
                .map_err(|e| failed("adding a file comment", e))?;
            if added.add_pull_request_review_thread.thread.id.is_empty() {
                return Err(failed("adding a file comment", "no thread was added"));
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

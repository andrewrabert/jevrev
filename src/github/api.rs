use anyhow::{anyhow, bail};
use std::collections::{BTreeMap, HashMap};
use std::io::Read;
use std::num::{NonZeroU64, NonZeroUsize};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use bytes::Bytes;
use futures::StreamExt;
use http_body_util::BodyExt;
use http_body_util::combinators::BoxBody;
use octocrab::Octocrab;

use crate::Error;
use crate::files::{InMemory, ReadTextError};
use crate::git::{self, ContentPair, Diff, GitObjectId, GitRevision, Side};
use crate::github::credentials::Token;
use crate::github::pull_request::{GithubPullRequest, PullRequest, User};
use crate::github::repo::{GithubHost, GithubRepo};
use crate::github::review;
use crate::ignore_rules::Ignore;
use crate::source_path::{SourcePath, SourcePathError};
use crate::store::{Finding, Review};

const DEFAULT_HOST: &str = "github.com";
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
    host: GithubHost,
    rest: Octocrab,
    graphql: Octocrab,
    rest_base: url::Url,
}

pub struct Blobs {
    client: Client,
    repo: GithubRepo,
    blobs: BTreeMap<String, GitObjectId>,
    read: Mutex<HashMap<String, String>>,
}

impl Blobs {
    pub fn paths(&self, ignore: Option<&Ignore>) -> Result<Vec<SourcePath>, Error> {
        let mut found = Vec::new();
        for text in self.blobs.keys() {
            match SourcePath::new(text, ignore) {
                Ok(path) => found.push(path),
                Err(SourcePathError::Ignored) => {}
                Err(e) => return Err(Error::from(e).context(text.clone())),
            }
        }
        Ok(found)
    }

    pub async fn read_text(&self, path: &SourcePath) -> Result<String, ReadTextError> {
        let path = path.to_str();
        let cached = self.cache()?.get(path).cloned();
        if let Some(found) = cached {
            return Ok(found);
        }
        let sha = self
            .blobs
            .get(path)
            .ok_or_else(|| anyhow!("no such file"))?;
        let text = String::from_utf8(self.client.blob(&self.repo, sha).await?)?;
        self.cache()?.insert(path.to_string(), text.clone());
        Ok(text)
    }

    fn cache(&self) -> Result<std::sync::MutexGuard<'_, HashMap<String, String>>, Error> {
        self.read
            .lock()
            .map_err(|_| anyhow!("a thread panicked while it held the read files"))
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
            let entry_path = entry.path().map_err(broken)?.into_owned();
            let archive_folder: PathBuf = entry_path.components().take(1).collect();
            let path = match SourcePath::from_base(&archive_folder, &entry_path, None) {
                Ok(path) => path,
                Err(SourcePathError::InsideGitDirectory) => continue,
                Err(e) => return Err(Error::from(e).context(entry_path.display().to_string())),
            };
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
                Some(sha) => Ok::<_, ReadTextError>(Side::Present(String::from_utf8(
                    self.blob(repo, &sha).await?,
                )?)),
            }
        };
        let read_new = |sha: GitObjectId| async move {
            Ok::<_, ReadTextError>(String::from_utf8(self.blob(repo, &sha).await?)?)
        };
        let read_pairs: Vec<_> = futures::stream::iter(changed)
            .map(|(text, before, after)| async move {
                let path = SourcePath::new(&text, None).map_err(|e| Error::from(e).context(text));
                match path {
                    Ok(path) => Ok((
                        path,
                        futures::future::try_join(read(before), read_new(after)).await,
                    )),
                    Err(e) => Err(e),
                }
            })
            .buffered(BLOB_CONCURRENCY / 2)
            .collect()
            .await;
        let mut pairs = Vec::new();
        for read in read_pairs {
            let (path, sides) = read?;
            match sides {
                Ok((old, new)) => pairs.push(ContentPair { path, old, new }),
                Err(ReadTextError::NotUtf8(e)) => {
                    tracing::debug!("skipping {path}: not UTF-8 text: {e}");
                }
                Err(ReadTextError::Read(e)) => return Err(e.context(path.to_string())),
            }
        }
        git::diff_contents(&pairs, context, inter_hunk_context)
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

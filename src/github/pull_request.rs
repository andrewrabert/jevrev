use std::num::NonZeroU64;

use crate::git::GitObjectId;

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GithubPullRequest {
    pub base: GitObjectId,
    pub head: GitObjectId,
    pub number: NonZeroU64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct User {
    pub login: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PullRequest {
    pub author: User,
    pub head: GitObjectId,
    pub target: GitObjectId,
}

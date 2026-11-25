use anyhow::Context;

use crate::Error;
use crate::git::GitObjectId;
use crate::github::credentials::Token;
use crate::github::repo::GithubRepo;
use crate::memgit::{Pack, Remote};

pub async fn fetch(
    repo: &GithubRepo,
    token: Option<&Token>,
    commits: &[&GitObjectId],
) -> Result<Pack, Error> {
    let url = format!(
        "https://{}/{}/{}.git",
        repo.host(),
        repo.name().org(),
        repo.name().name()
    );
    let token = token.map(|token| token.as_str().to_string());
    let commits: Vec<gix::hash::ObjectId> = commits.iter().map(|&c| c.into()).collect();
    let name = format!("{}/{}", repo.host(), repo.name());
    tokio::task::spawn_blocking(move || {
        Remote::at(url.as_str())?
            .with_credentials(move |action| github_token(action, token.as_deref()))
            .fetch(&commits)
    })
    .await?
    .with_context(|| format!("fetching {name}"))
}

fn github_token(
    action: gix::credentials::helper::Action,
    token: Option<&str>,
) -> gix::Result<Option<gix::credentials::protocol::Outcome>> {
    match (action, token) {
        (gix::credentials::helper::Action::Get(context), Some(token)) => {
            Ok(Some(gix::credentials::protocol::Outcome {
                identity: gix::sec::identity::Account {
                    username: "x-access-token".to_string(),
                    password: token.to_string(),
                    oauth_refresh_token: None,
                },
                next: context.into(),
            }))
        }
        _ => Ok(None),
    }
}

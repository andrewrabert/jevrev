use anyhow::{anyhow, bail};
use std::ffi::OsStr;
use std::sync::LazyLock;

use crate::Error;

const HOST_PATTERN: &str = r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$";

static HOST: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(HOST_PATTERN).expect("a valid host pattern"));

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GithubHost {
    text: String,
    url: url::Url,
}

impl GithubHost {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<GithubHost, Error> {
        let value = value.as_ref();
        let text = value
            .to_str()
            .ok_or_else(|| anyhow!("host: {value:?} is not UTF-8"))?;
        if !HOST.is_match(text) {
            bail!("host: {text:?} does not match {HOST_PATTERN}");
        }
        let url = url::Url::parse(&format!("https://{text}/"))
            .map_err(|e| anyhow!("host: {text:?}: {e}"))?;
        Ok(GithubHost {
            text: text.to_string(),
            url,
        })
    }

    pub fn as_str(&self) -> &str {
        &self.text
    }

    pub fn url(&self) -> &url::Url {
        &self.url
    }
}

impl std::fmt::Display for GithubHost {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.text)
    }
}

impl serde::Serialize for GithubHost {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.text)
    }
}

impl<'de> serde::Deserialize<'de> for GithubHost {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<GithubHost, D::Error> {
        GithubHost::try_new(String::deserialize(deserializer)?).map_err(serde::de::Error::custom)
    }
}

#[nutype::nutype(
    validate(predicate = |org| !org.is_empty()
        && org != "."
        && org != ".."
        && org.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))),
    derive(Debug, Clone, PartialEq, Eq, Display, Deref)
)]
pub struct GithubOrg(String);

#[nutype::nutype(
    validate(predicate = |name| !name.is_empty()
        && name != "."
        && name != ".."
        && name.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '_'))),
    derive(Debug, Clone, PartialEq, Eq, Display, Deref)
)]
pub struct GithubName(String);

/// A repo's ORG/NAME.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GithubRepoName {
    org: GithubOrg,
    name: GithubName,
}

impl GithubRepoName {
    pub fn parse(value: impl AsRef<OsStr>) -> Result<GithubRepoName, Error> {
        let value = value.as_ref();
        let text = value
            .to_str()
            .ok_or_else(|| anyhow!("{value:?} is not UTF-8"))?;
        let Some((org, name)) = text.split_once('/') else {
            bail!("{text}: expected ORG/NAME");
        };
        Ok(GithubRepoName {
            org: GithubOrg::try_new(org.to_string())
                .map_err(|_| anyhow!("{text}: org: invalid github name"))?,
            name: GithubName::try_new(name.to_string())
                .map_err(|_| anyhow!("{text}: name: invalid github name"))?,
        })
    }

    pub fn org(&self) -> &GithubOrg {
        &self.org
    }

    pub fn name(&self) -> &GithubName {
        &self.name
    }
}

impl std::fmt::Display for GithubRepoName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}/{}", self.org, self.name)
    }
}

impl serde::Serialize for GithubRepoName {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

impl<'de> serde::Deserialize<'de> for GithubRepoName {
    fn deserialize<D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> Result<GithubRepoName, D::Error> {
        GithubRepoName::parse(String::deserialize(deserializer)?).map_err(serde::de::Error::custom)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct GithubRepo {
    host: GithubHost,
    name: GithubRepoName,
}

impl GithubRepo {
    pub fn new(host: GithubHost, name: GithubRepoName) -> GithubRepo {
        GithubRepo { host, name }
    }

    pub fn host(&self) -> &GithubHost {
        &self.host
    }

    pub fn name(&self) -> &GithubRepoName {
        &self.name
    }
}

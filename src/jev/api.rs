use anyhow::{Context, anyhow, bail};
use std::ffi::OsStr;
use std::sync::LazyLock;
use std::time::Duration;

use indexmap::IndexMap;
use reqwest_middleware::ClientWithMiddleware;
use reqwest_retry::RetryTransientMiddleware;
use reqwest_retry::policies::ExponentialBackoff;

use crate::Error;
use crate::jev::ask::Noul;

const MAX_RETRIES: u32 = 2;
const MAX_ERROR_BODY_LENGTH: usize = 200;
const MAX_CONTEXT_TOKENS: usize = 32_000;

static NON_BLANK: LazyLock<regex::Regex> =
    LazyLock::new(|| regex::Regex::new(r"\S").expect("a valid pattern"));

#[derive(Clone, PartialEq, Eq)]
pub struct ApiKey(String);

impl ApiKey {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<ApiKey, Error> {
        let value = value.as_ref();
        let value = value
            .to_str()
            .ok_or_else(|| anyhow!("the API key is not UTF-8"))?;
        if value.is_empty() {
            bail!("the API key is empty");
        }
        Ok(ApiKey(value.to_string()))
    }
}

impl std::fmt::Debug for ApiKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ApiKey(..)")
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BaseUrl(url::Url);

impl Default for BaseUrl {
    fn default() -> BaseUrl {
        BaseUrl(url::Url::parse("https://api.typesafe.ai").expect("a valid URL"))
    }
}

impl BaseUrl {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<BaseUrl, Error> {
        let value = value.as_ref();
        let value = value
            .to_str()
            .ok_or_else(|| anyhow!("the base URL: {value:?} is not UTF-8"))?;
        match url::Url::parse(value) {
            Ok(url) if url.has_host() => Ok(BaseUrl(url)),
            _ => bail!("{value:?} is not an absolute URL"),
        }
    }

    fn systemone(&self) -> url::Url {
        let mut url = self.0.clone();
        url.path_segments_mut()
            .expect("a URL with a host has path segments")
            .pop_if_empty()
            .extend(["v1", "systemone"]);
        url
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Timeout(Option<Duration>);

impl Default for Timeout {
    fn default() -> Timeout {
        Timeout(Some(Duration::from_secs(90)))
    }
}

impl Timeout {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<Timeout, Error> {
        let value = value.as_ref();
        let value = value
            .to_str()
            .ok_or_else(|| anyhow!("the timeout: {value:?} is not UTF-8"))?;
        match value.parse::<f64>() {
            Ok(seconds) if seconds > 0.0 => Ok(Timeout(Duration::try_from_secs_f64(seconds).ok())),
            _ => bail!("{value:?} is not a positive number"),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(transparent)]
pub struct Model(String);

impl Default for Model {
    fn default() -> Model {
        Model("jev-latest".to_string())
    }
}

impl Model {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<Model, Error> {
        let value = value.as_ref();
        let value = value
            .to_str()
            .ok_or_else(|| anyhow!("the model: {value:?} is not UTF-8"))?;
        if value.is_empty() {
            bail!("the model is empty");
        }
        Ok(Model(value.to_string()))
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct NoulAnswer {
    pub question: String,
    pub probability: f64,
}

#[derive(Clone)]
pub struct Client {
    http: ClientWithMiddleware,
    api_key: ApiKey,
    url: url::Url,
    model: Model,
}

#[derive(serde::Serialize)]
struct AskRequestBody<'r> {
    state: &'r str,
    model: &'r Model,
    questions: &'r IndexMap<String, Noul>,
}

#[derive(serde::Deserialize)]
struct Answers {
    answers: IndexMap<String, Answer>,
}

#[derive(serde::Deserialize)]
#[serde(tag = "type", rename_all = "lowercase")]
enum Answer {
    Noul { noul: f64 },
}

impl Client {
    pub fn new(
        api_key: ApiKey,
        base_url: &BaseUrl,
        model: Model,
        timeout: Timeout,
    ) -> Result<Client, Error> {
        let mut builder = reqwest::Client::builder()
            .user_agent(format!("{}/{}", crate::APP_NAME, crate::APP_VERSION))
            .redirect(reqwest::redirect::Policy::none());
        if let Timeout(Some(timeout)) = timeout {
            builder = builder.connect_timeout(timeout).read_timeout(timeout);
        }
        let http = builder.build().with_context(|| "HTTP".to_string())?;
        let policy = ExponentialBackoff::builder()
            .retry_bounds(Duration::from_millis(500), Duration::from_secs(5))
            .build_with_max_retries(MAX_RETRIES);
        Ok(Client {
            http: reqwest_middleware::ClientBuilder::new(http)
                .with(RetryTransientMiddleware::new_with_policy(policy))
                .build(),
            api_key,
            url: base_url.systemone(),
            model,
        })
    }

    pub async fn ask(
        &self,
        questions: &IndexMap<String, Noul>,
        state: &str,
    ) -> Result<Vec<NoulAnswer>, Error> {
        let mut problems = Vec::new();
        if !NON_BLANK.is_match(state) {
            problems.push("state: is blank".to_string());
        }
        if questions.is_empty() {
            problems.push("questions: none given".into());
        }
        let body = serde_json::to_vec(&AskRequestBody {
            state,
            model: &self.model,
            questions,
        })
        .expect("a request serializes");
        if problems.is_empty() {
            let tokens = body.len() / 4 + 1;
            if tokens > MAX_CONTEXT_TOKENS {
                problems.push(format!(
                    "request: roughly {tokens} estimated tokens cannot fit the \
                     {MAX_CONTEXT_TOKENS}-token limit; split the state, or drop rules that do \
                     not need the full context"
                ));
            }
        }
        if !problems.is_empty() {
            let count = problems.len();
            let mut lines: Vec<String> = problems
                .into_iter()
                .map(|p| format!("error: {p}"))
                .collect();
            lines.push(format!(
                "{count} problem(s) would cause a failed or wasted call; fix them"
            ));
            bail!(lines.join("\n"));
        }
        let endpoint = format!("POST {}", self.url);
        let failed = |e: &dyn std::fmt::Display| anyhow!("{endpoint}: {e}");
        let response = self
            .http
            .post(self.url.clone())
            .bearer_auth(&self.api_key.0)
            .header(reqwest::header::ACCEPT, "application/json")
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(body)
            .send()
            .await
            .map_err(|e| failed(&e))?;
        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.map_err(|e| failed(&e))?;
            let body: String = body.trim().chars().take(MAX_ERROR_BODY_LENGTH).collect();
            return Err(failed(&format!("{status} {body}")));
        }
        let body = response.bytes().await.map_err(|e| failed(&e))?;
        let parsed: Answers = serde_json::from_slice(&body).map_err(|e| failed(&e))?;
        Ok(parsed
            .answers
            .into_iter()
            .map(|(question, Answer::Noul { noul })| NoulAnswer {
                question,
                probability: noul,
            })
            .collect())
    }
}

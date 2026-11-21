use anyhow::{Context, anyhow, bail};
use std::ffi::OsStr;
use std::time::Duration;

use indexmap::IndexMap;
use reqwest_middleware::ClientWithMiddleware;
use reqwest_retry::RetryTransientMiddleware;
use reqwest_retry::policies::ExponentialBackoff;

use crate::Error;
use crate::jev::ask::{Criteria, Instructions};
use crate::jev::cache::{Cache, CacheKey};

const MAX_RETRIES: u32 = 2;
const MAX_ERROR_BODY_LENGTH: usize = 200;

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
pub(crate) struct AskRequest {
    pub(crate) url: url::Url,
    pub(crate) body: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct NoulAnswer {
    pub question: String,
    pub probability: Probability,
}

#[derive(Clone)]
pub struct Client {
    http: ClientWithMiddleware,
    api_key: ApiKey,
    url: url::Url,
    model: Model,
    cache: Option<Cache>,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct SystemOneRequest {
    pub model: Model,
    pub state: String,
    pub questions: Questions,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct Questions(IndexMap<String, Question>);

impl Questions {
    pub fn try_new(questions: IndexMap<String, Question>) -> Result<Questions, Error> {
        if questions.is_empty() {
            bail!("questions: none given");
        }
        Ok(Questions(questions))
    }

    pub fn contains(&self, name: &str) -> bool {
        self.0.contains_key(name)
    }

    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.0.keys().map(String::as_str)
    }
}

impl PartialEq for Questions {
    fn eq(&self, other: &Questions) -> bool {
        self.0.iter().eq(other.0.iter())
    }
}

#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum Question {
    Noul {
        instructions: Instructions,
        #[serde(skip_serializing_if = "Option::is_none")]
        criteria: Option<Criteria>,
    },
}

#[nutype::nutype(
    validate(finite, greater_or_equal = 0.0, less_or_equal = 1.0),
    derive(
        Debug,
        Clone,
        Copy,
        PartialEq,
        PartialOrd,
        Serialize,
        Deserialize,
        Into
    )
)]
pub struct Probability(f64);

#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SystemOneResponse {
    model: String,
    answers: IndexMap<String, Answer>,
    usage: Usage,
}

impl SystemOneResponse {
    pub fn model(&self) -> &str {
        &self.model
    }

    pub fn answers(&self) -> &IndexMap<String, Answer> {
        &self.answers
    }

    pub fn usage(&self) -> Usage {
        self.usage
    }
}

#[derive(Debug, Clone, PartialEq, serde::Deserialize)]
#[serde(tag = "type", rename_all = "lowercase", deny_unknown_fields)]
pub enum Answer {
    Noul { noul: Probability },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
}

impl Client {
    pub fn new(
        api_key: ApiKey,
        base_url: &BaseUrl,
        model: Model,
        timeout: Timeout,
        cache: Option<Cache>,
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
            cache,
        })
    }

    pub async fn ask(&self, questions: Questions, state: String) -> Result<NoulAnswers, Error> {
        let request = SystemOneRequest {
            model: self.model.clone(),
            state,
            questions,
        };
        let ask_request = AskRequest {
            url: self.url.clone(),
            body: serde_json::to_string(&request).expect("a request serializes"),
        };
        let answers = |response: &SystemOneResponse| {
            NoulAnswers::try_new(response, &request.questions)
                .map_err(|e| anyhow!("POST {}: {e}", self.url))
        };
        let key = CacheKey::new(&ask_request, &request.questions);
        if let Some(cache) = &self.cache
            && let Some(answers) = cache.get(&key).await?
        {
            return Ok(answers);
        }
        let response = self.ask_request(&ask_request).await?;
        let answers = answers(&response)?;
        if let Some(cache) = &self.cache {
            cache.put(&key, &answers);
        }
        Ok(answers)
    }

    pub fn finish(&self) -> Result<(), Error> {
        match &self.cache {
            Some(cache) => cache.finish(),
            None => Ok(()),
        }
    }

    async fn ask_request(&self, request: &AskRequest) -> Result<SystemOneResponse, Error> {
        let endpoint = format!("POST {}", request.url);
        let failed = |e: &dyn std::fmt::Display| anyhow!("{endpoint}: {e}");
        let response = self
            .http
            .post(request.url.clone())
            .bearer_auth(&self.api_key.0)
            .header(reqwest::header::ACCEPT, "application/json")
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(request.body.clone())
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
        serde_json::from_slice(&body).map_err(|e| failed(&e))
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct NoulAnswers(Vec<NoulAnswer>);

impl NoulAnswers {
    pub fn try_new(
        response: &SystemOneResponse,
        questions: &Questions,
    ) -> Result<NoulAnswers, Error> {
        if let Some(name) = response.answers.keys().find(|n| !questions.contains(n)) {
            bail!("answers: {name:?} is not a question that was asked");
        }
        questions
            .names()
            .map(|name| match response.answers.get(name) {
                Some(Answer::Noul { noul }) => Ok(NoulAnswer {
                    question: name.to_string(),
                    probability: *noul,
                }),
                None => bail!("answers: question {name:?} has no answer"),
            })
            .collect::<Result<_, _>>()
            .map(NoulAnswers)
    }

    pub(crate) fn from_cached(
        probabilities: Vec<Probability>,
        questions: &Questions,
    ) -> Result<NoulAnswers, Error> {
        let count = questions.names().count();
        if probabilities.len() != count {
            bail!(
                "answers: {} probabilities for {count} questions",
                probabilities.len()
            );
        }
        Ok(NoulAnswers(
            questions
                .names()
                .zip(probabilities)
                .map(|(name, probability)| NoulAnswer {
                    question: name.to_string(),
                    probability,
                })
                .collect(),
        ))
    }

    pub fn iter(&self) -> impl Iterator<Item = &NoulAnswer> {
        self.0.iter()
    }

    pub(crate) fn probabilities(&self) -> Vec<Probability> {
        self.0.iter().map(|answer| answer.probability).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(body: &str) -> serde_json::Result<SystemOneResponse> {
        serde_json::from_str(body)
    }

    fn body(answers: &str) -> String {
        format!(
            r#"{{"model": "jev-1.14.0", "answers": {answers}, "usage": {{"input_tokens": 120, "output_tokens": 12}}}}"#
        )
    }

    #[test]
    fn parses_a_full_response() {
        let response = parse(&body(r#"{"q1": {"type": "noul", "noul": 0.98}}"#)).unwrap();
        assert_eq!(response.model(), "jev-1.14.0");
        assert_eq!(
            *response.answers(),
            IndexMap::from([(
                "q1".to_string(),
                Answer::Noul {
                    noul: Probability::try_new(0.98).unwrap()
                }
            )])
        );
        assert_eq!(
            response.usage(),
            Usage {
                input_tokens: 120,
                output_tokens: 12
            }
        );
    }

    #[test]
    fn parses_the_probability_bounds() {
        for noul in ["0", "1", "0.0", "1.0"] {
            let answers = format!(r#"{{"q1": {{"type": "noul", "noul": {noul}}}}}"#);
            assert!(parse(&body(&answers)).is_ok(), "{noul}");
        }
    }

    #[test]
    fn a_probability_out_of_range_fails_to_construct() {
        for noul in ["-0.01", "1.01", "2"] {
            let answers = format!(r#"{{"q1": {{"type": "noul", "noul": {noul}}}}}"#);
            assert!(parse(&body(&answers)).is_err(), "{noul}");
        }
    }

    #[test]
    fn a_response_missing_a_field_fails_to_construct() {
        for body in [
            r#"{"answers": {"q1": {"type": "noul", "noul": 0.5}}, "usage": {"input_tokens": 1, "output_tokens": 1}}"#,
            r#"{"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}"#,
            r#"{"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.5}}}"#,
            r#"{"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.5}}, "usage": {"input_tokens": 1}}"#,
            r#"{"model": "m", "answers": {"q1": {"type": "noul"}}, "usage": {"input_tokens": 1, "output_tokens": 1}}"#,
        ] {
            assert!(parse(body).is_err(), "{body}");
        }
    }

    #[test]
    fn a_response_with_an_unknown_field_fails_to_construct() {
        for body in [
            r#"{"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.5}}, "usage": {"input_tokens": 1, "output_tokens": 1}, "fart": 1}"#,
            r#"{"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.5, "fart": 1}}, "usage": {"input_tokens": 1, "output_tokens": 1}}"#,
            r#"{"model": "m", "answers": {"q1": {"type": "noul", "noul": 0.5}}, "usage": {"input_tokens": 1, "output_tokens": 1, "fart": 1}}"#,
            r#"{"model": "m", "answers": {"q1": {"type": "choice", "choice": "a"}}, "usage": {"input_tokens": 1, "output_tokens": 1}}"#,
        ] {
            assert!(parse(body).is_err(), "{body}");
        }
    }

    fn written(request: &SystemOneRequest) -> serde_json::Value {
        serde_json::to_value(request).unwrap()
    }

    fn noul(criteria: Option<Criteria>) -> Questions {
        Questions::try_new(IndexMap::from([(
            "q1".to_string(),
            Question::Noul {
                instructions: Instructions {
                    task: "t".into(),
                    flags: "f".into(),
                },
                criteria,
            },
        )]))
        .unwrap()
    }

    #[test]
    fn a_request_serializes_as_documented() {
        let request = SystemOneRequest {
            model: Model::try_new("m").unwrap(),
            state: "s".into(),
            questions: noul(Some(Criteria {
                when_true: "y".into(),
                when_false: "n".into(),
            })),
        };
        assert_eq!(
            written(&request),
            serde_json::json!({
                "model": "m",
                "state": "s",
                "questions": {"q1": {
                    "type": "noul",
                    "instructions": {"task": "t", "flags": "f"},
                    "criteria": {"true": "y", "false": "n"},
                }},
            })
        );
    }

    #[test]
    fn a_question_without_criteria_serializes_without_them() {
        let request = SystemOneRequest {
            model: Model::try_new("m").unwrap(),
            state: "s".into(),
            questions: noul(None),
        };
        assert_eq!(
            written(&request)["questions"]["q1"],
            serde_json::json!({"type": "noul", "instructions": {"task": "t", "flags": "f"}})
        );
    }

    #[test]
    fn no_questions_fail_to_construct() {
        assert!(Questions::try_new(IndexMap::new()).is_err());
    }

    #[test]
    fn questions_in_another_order_are_not_equal() {
        let question = |flags: &str| Question::Noul {
            instructions: Instructions {
                task: "t".into(),
                flags: flags.into(),
            },
            criteria: None,
        };
        let forward = Questions::try_new(IndexMap::from([
            ("q1".to_string(), question("a")),
            ("q2".to_string(), question("b")),
        ]))
        .unwrap();
        let backward = Questions::try_new(IndexMap::from([
            ("q2".to_string(), question("b")),
            ("q1".to_string(), question("a")),
        ]))
        .unwrap();
        assert_ne!(forward, backward);
    }
}

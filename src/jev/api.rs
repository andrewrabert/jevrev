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
        Client::build(api_key, base_url, model, timeout, cache, None)
    }

    #[cfg(test)]
    fn with_transport(
        api_key: ApiKey,
        base_url: &BaseUrl,
        model: Model,
        cache: Option<Cache>,
        transport: std::sync::Arc<dyn reqwest_middleware::Middleware>,
    ) -> Result<Client, Error> {
        Client::build(
            api_key,
            base_url,
            model,
            Timeout::default(),
            cache,
            Some(transport),
        )
    }

    fn build(
        api_key: ApiKey,
        base_url: &BaseUrl,
        model: Model,
        timeout: Timeout,
        cache: Option<Cache>,
        transport: Option<std::sync::Arc<dyn reqwest_middleware::Middleware>>,
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
        let mut http = reqwest_middleware::ClientBuilder::new(http)
            .with(RetryTransientMiddleware::new_with_policy(policy));
        if let Some(transport) = transport {
            http = http.with_arc(transport);
        }
        Ok(Client {
            http: http.build(),
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

    use std::path::Path;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use redb::{ConcurrencyMode, Database, ReadableDatabase, ReadableTable, TableDefinition};

    use crate::jev::api::{
        ApiKey, BaseUrl, Client, Model, NoulAnswer, Probability, Question, Questions,
    };
    use crate::jev::ask::Instructions;
    use crate::jev::cache::{Cache, CacheFile};

    const RESPONSE: &str = r#"{"model": "jev-1.14.0", "answers": {"q1": {"type": "noul", "noul": 0.25}}, "usage": {"input_tokens": 10, "output_tokens": 1}}"#;

    type Transport = Arc<dyn reqwest_middleware::Middleware>;

    fn middleware<F>(answer: F) -> F
    where
        F: for<'a> Fn(
            reqwest::Request,
            &'a mut http::Extensions,
            reqwest_middleware::Next<'a>,
        ) -> futures::future::BoxFuture<
            'a,
            reqwest_middleware::Result<reqwest::Response>,
        >,
    {
        answer
    }

    fn serve() -> (Transport, Arc<AtomicUsize>) {
        let requests = Arc::new(AtomicUsize::new(0));
        let counted = Arc::clone(&requests);
        let transport = middleware(move |_, _, _| {
            counted.fetch_add(1, Ordering::SeqCst);
            let response = http::Response::builder()
                .status(200)
                .header("content-type", "application/json")
                .body(RESPONSE)
                .expect("a canned response builds");
            Box::pin(async move { Ok(reqwest::Response::from(response)) })
        });
        (Arc::new(transport), requests)
    }

    fn client(transport: &Transport, cache: Option<&Path>) -> Client {
        let _ = rustls::crypto::ring::default_provider().install_default();
        Client::with_transport(
            ApiKey::try_new("key").unwrap(),
            &BaseUrl::try_new("https://jev.test").unwrap(),
            Model::try_new("fartburger").unwrap(),
            cache.map(|dir| {
                Cache::open(&CacheFile::try_new(dir.join("cache.redb")).unwrap()).unwrap()
            }),
            Arc::clone(transport),
        )
        .unwrap()
    }

    fn questions() -> Questions {
        questions_named(&["q1"])
    }

    fn questions_named(names: &[&str]) -> Questions {
        Questions::try_new(
            names
                .iter()
                .map(|name| {
                    let question = Question::Noul {
                        instructions: Instructions {
                            task: "task".into(),
                            flags: "flags".into(),
                        },
                        criteria: None,
                    };
                    (name.to_string(), question)
                })
                .collect(),
        )
        .unwrap()
    }

    fn answers() -> Vec<NoulAnswer> {
        vec![NoulAnswer {
            question: "q1".into(),
            probability: Probability::try_new(0.25).unwrap(),
        }]
    }

    async fn ask(client: &Client, state: &str) -> Vec<NoulAnswer> {
        client
            .ask(questions(), state.into())
            .await
            .unwrap()
            .iter()
            .cloned()
            .collect()
    }

    const ANSWERS: TableDefinition<&[u8], &[u8]> =
        TableDefinition::new("ask_response_probabilities");

    fn database(dir: &Path) -> Database {
        let mut builder = Database::builder();
        builder.set_concurrency_mode(ConcurrencyMode::MultiWriter);
        builder.create(dir.join("cache.redb")).unwrap()
    }

    fn entries(dir: &Path) -> Vec<(Vec<u8>, Vec<u8>)> {
        let db = database(dir);
        let read = db.begin_read().unwrap();
        let table = match read.open_table(ANSWERS) {
            Ok(table) => table,
            Err(redb::TableError::TableDoesNotExist(_)) => return Vec::new(),
            Err(e) => panic!("{e}"),
        };
        table
            .iter()
            .unwrap()
            .map(|entry| {
                let (key, value) = entry.unwrap();
                (key.value().to_vec(), value.value().to_vec())
            })
            .collect()
    }

    #[tokio::test]
    async fn a_cached_request_is_not_asked_again() {
        let (transport, requests) = serve();
        let dir = tempfile::tempdir().unwrap();
        let first = client(&transport, Some(dir.path()));
        assert_eq!(ask(&first, "state").await, answers());
        first.finish().unwrap();
        drop(first);
        let second = client(&transport, Some(dir.path()));
        assert_eq!(ask(&second, "state").await, answers());
        assert_eq!(requests.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn each_request_has_its_own_entry() {
        let (transport, requests) = serve();
        let dir = tempfile::tempdir().unwrap();
        let client = client(&transport, Some(dir.path()));
        ask(&client, "one").await;
        ask(&client, "two").await;
        client.finish().unwrap();
        drop(client);
        assert_eq!(entries(dir.path()).len(), 2);
        assert_eq!(requests.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn concurrent_requests_are_all_written() {
        let (transport, requests) = serve();
        let dir = tempfile::tempdir().unwrap();
        let client = client(&transport, Some(dir.path()));
        let states: Vec<String> = (0..20).map(|n| format!("state {n}")).collect();
        futures::future::join_all(states.iter().map(|state| ask(&client, state))).await;
        client.finish().unwrap();
        drop(client);
        assert_eq!(entries(dir.path()).len(), 20);
        assert_eq!(requests.load(Ordering::SeqCst), 20);
    }

    #[tokio::test]
    async fn no_cache_reads_and_writes_nothing() {
        let (transport, requests) = serve();
        let dir = tempfile::tempdir().unwrap();
        let cached = client(&transport, Some(dir.path()));
        ask(&cached, "state").await;
        cached.finish().unwrap();
        drop(cached);
        let uncached = client(&transport, None);
        assert_eq!(ask(&uncached, "state").await, answers());
        assert_eq!(ask(&uncached, "two").await, answers());
        uncached.finish().unwrap();
        assert_eq!(entries(dir.path()).len(), 1);
        assert_eq!(requests.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn an_entry_that_does_not_decode_is_asked_and_rewritten() {
        let (transport, requests) = serve();
        let dir = tempfile::tempdir().unwrap();
        let first = client(&transport, Some(dir.path()));
        ask(&first, "state").await;
        first.finish().unwrap();
        drop(first);
        let [(key, written)] = entries(dir.path()).try_into().unwrap();
        {
            let db = database(dir.path());
            let write = db.begin_write().unwrap();
            write
                .open_table(ANSWERS)
                .unwrap()
                .insert(key.as_slice(), b"not postcard".as_slice())
                .unwrap();
            write.commit().unwrap();
        }
        let second = client(&transport, Some(dir.path()));
        assert_eq!(ask(&second, "state").await, answers());
        second.finish().unwrap();
        drop(second);
        assert_eq!(requests.load(Ordering::SeqCst), 2);
        assert_eq!(entries(dir.path()), [(key, written)]);
    }

    #[tokio::test]
    async fn an_entry_with_the_wrong_number_of_answers_is_an_error() {
        let (transport, requests) = serve();
        let dir = tempfile::tempdir().unwrap();
        let first = client(&transport, Some(dir.path()));
        ask(&first, "state").await;
        first.finish().unwrap();
        drop(first);
        let [(key, _)] = entries(dir.path()).try_into().unwrap();
        {
            let db = database(dir.path());
            let write = db.begin_write().unwrap();
            let two = postcard::to_stdvec(&vec![0.25_f64, 0.5]).unwrap();
            write
                .open_table(ANSWERS)
                .unwrap()
                .insert(key.as_slice(), two.as_slice())
                .unwrap();
            write.commit().unwrap();
        }
        let second = client(&transport, Some(dir.path()));
        let error = second.ask(questions(), "state".into()).await.unwrap_err();
        assert!(
            format!("{error:#}").contains("2 probabilities for 1 questions"),
            "{error:#}"
        );
        assert_eq!(requests.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn a_cache_table_of_other_types_is_an_error_on_open() {
        let dir = tempfile::tempdir().unwrap();
        {
            let db = database(dir.path());
            let write = db.begin_write().unwrap();
            let other: TableDefinition<u64, u64> =
                TableDefinition::new("ask_response_probabilities");
            write.open_table(other).unwrap().insert(1, 2).unwrap();
            write.commit().unwrap();
        }
        let error = Cache::open(&CacheFile::try_new(dir.path().join("cache.redb")).unwrap())
            .err()
            .unwrap();
        assert!(format!("{error:#}").contains("cache.redb"), "{error:#}");
    }

    #[tokio::test]
    async fn an_entry_is_the_probabilities_under_the_request_hash() {
        let (transport, _) = serve();
        let dir = tempfile::tempdir().unwrap();
        let client = client(&transport, Some(dir.path()));
        ask(&client, "state").await;
        client.finish().unwrap();
        drop(client);
        let [(key, value)] = entries(dir.path()).try_into().unwrap();
        assert_eq!(key.len(), 32);
        assert_eq!(postcard::from_bytes::<Vec<f64>>(&value).unwrap(), [0.25]);
        assert_eq!(value.len(), 1 + 8);
    }

    #[test]
    fn a_cache_that_cannot_open_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("file");
        std::fs::write(&file, "").unwrap();
        let error = Cache::open(&CacheFile::try_new(file.join("cache.redb")).unwrap())
            .err()
            .unwrap();
        assert!(format!("{error:#}").contains("file"), "{error:#}");
    }

    #[test]
    fn an_empty_cache_file_is_an_error() {
        assert!(CacheFile::try_new("").is_err());
    }

    #[tokio::test]
    async fn a_response_without_an_answer_to_a_question_is_an_error_and_not_cached() {
        let (transport, _) = serve();
        let dir = tempfile::tempdir().unwrap();
        let client = client(&transport, Some(dir.path()));
        let error = client
            .ask(questions_named(&["q1", "q2"]), "state".into())
            .await
            .unwrap_err();
        assert!(
            format!("{error:#}").contains("\"q2\" has no answer"),
            "{error:#}"
        );
        client.finish().unwrap();
        drop(client);
        assert!(entries(dir.path()).is_empty());
    }

    #[tokio::test]
    async fn a_response_answering_no_question_is_an_error_and_not_cached() {
        let (transport, _) = serve();
        let dir = tempfile::tempdir().unwrap();
        let client = client(&transport, Some(dir.path()));
        let error = client
            .ask(questions_named(&["q2"]), "state".into())
            .await
            .unwrap_err();
        assert!(
            format!("{error:#}").contains("\"q1\" is not a question that was asked"),
            "{error:#}"
        );
        client.finish().unwrap();
        drop(client);
        assert!(entries(dir.path()).is_empty());
    }
}

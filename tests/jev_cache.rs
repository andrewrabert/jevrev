use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use jevrev::jev::api::{
    ApiKey, BaseUrl, Client, Model, NoulAnswer, Probability, Question, Questions, Timeout,
};
use jevrev::jev::ask::Instructions;
use jevrev::jev::cache::{Cache, CacheFile};
use redb::{ConcurrencyMode, Database, ReadableDatabase, ReadableTable, TableDefinition};

const RESPONSE: &str = r#"{"model": "jev-1.14.0", "answers": {"q1": {"type": "noul", "noul": 0.25}}, "usage": {"input_tokens": 10, "output_tokens": 1}}"#;

fn serve() -> (BaseUrl, String, Arc<AtomicUsize>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let url = BaseUrl::try_new(&origin).unwrap();
    let requests = Arc::new(AtomicUsize::new(0));
    let counted = Arc::clone(&requests);
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let mut stream = stream.unwrap();
            let mut received = Vec::new();
            let mut buffer = [0; 4096];
            let length = loop {
                let n = stream.read(&mut buffer).unwrap();
                received.extend_from_slice(&buffer[..n]);
                let text = String::from_utf8_lossy(&received);
                if let Some(end) = text.find("\r\n\r\n") {
                    let length: usize = text[..end]
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse().unwrap())
                        })
                        .unwrap_or(0);
                    break end + 4 + length;
                }
            };
            while received.len() < length {
                let n = stream.read(&mut buffer).unwrap();
                received.extend_from_slice(&buffer[..n]);
            }
            counted.fetch_add(1, Ordering::SeqCst);
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{RESPONSE}",
                RESPONSE.len()
            )
            .unwrap();
        }
    });
    (url, origin, requests)
}

fn client(url: &BaseUrl, cache: Option<&Path>) -> Client {
    let _ = rustls::crypto::ring::default_provider().install_default();
    Client::new(
        ApiKey::try_new("key").unwrap(),
        url,
        Model::try_new("fartburger").unwrap(),
        Timeout::default(),
        cache.map(|dir| Cache::open(&CacheFile::try_new(dir.join("cache.redb")).unwrap()).unwrap()),
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

const ANSWERS: TableDefinition<&[u8], &[u8]> = TableDefinition::new("ask_response_probabilities");

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
    let (url, _, requests) = serve();
    let dir = tempfile::tempdir().unwrap();
    let first = client(&url, Some(dir.path()));
    assert_eq!(ask(&first, "state").await, answers());
    first.finish().unwrap();
    drop(first);
    let second = client(&url, Some(dir.path()));
    assert_eq!(ask(&second, "state").await, answers());
    assert_eq!(requests.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn each_request_has_its_own_entry() {
    let (url, _, requests) = serve();
    let dir = tempfile::tempdir().unwrap();
    let client = client(&url, Some(dir.path()));
    ask(&client, "one").await;
    ask(&client, "two").await;
    client.finish().unwrap();
    drop(client);
    assert_eq!(entries(dir.path()).len(), 2);
    assert_eq!(requests.load(Ordering::SeqCst), 2);
}

#[tokio::test]
async fn concurrent_requests_are_all_written() {
    let (url, _, requests) = serve();
    let dir = tempfile::tempdir().unwrap();
    let client = client(&url, Some(dir.path()));
    let states: Vec<String> = (0..20).map(|n| format!("state {n}")).collect();
    futures::future::join_all(states.iter().map(|state| ask(&client, state))).await;
    client.finish().unwrap();
    drop(client);
    assert_eq!(entries(dir.path()).len(), 20);
    assert_eq!(requests.load(Ordering::SeqCst), 20);
}

#[tokio::test]
async fn no_cache_reads_and_writes_nothing() {
    let (url, _, requests) = serve();
    let dir = tempfile::tempdir().unwrap();
    let cached = client(&url, Some(dir.path()));
    ask(&cached, "state").await;
    cached.finish().unwrap();
    drop(cached);
    let uncached = client(&url, None);
    assert_eq!(ask(&uncached, "state").await, answers());
    assert_eq!(ask(&uncached, "two").await, answers());
    uncached.finish().unwrap();
    assert_eq!(entries(dir.path()).len(), 1);
    assert_eq!(requests.load(Ordering::SeqCst), 3);
}

#[tokio::test]
async fn an_entry_that_does_not_decode_is_asked_and_rewritten() {
    let (url, _, requests) = serve();
    let dir = tempfile::tempdir().unwrap();
    let first = client(&url, Some(dir.path()));
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
    let second = client(&url, Some(dir.path()));
    assert_eq!(ask(&second, "state").await, answers());
    second.finish().unwrap();
    drop(second);
    assert_eq!(requests.load(Ordering::SeqCst), 2);
    assert_eq!(entries(dir.path()), [(key, written)]);
}

#[tokio::test]
async fn an_entry_with_the_wrong_number_of_answers_is_an_error() {
    let (url, _, requests) = serve();
    let dir = tempfile::tempdir().unwrap();
    let first = client(&url, Some(dir.path()));
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
    let second = client(&url, Some(dir.path()));
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
        let other: TableDefinition<u64, u64> = TableDefinition::new("ask_response_probabilities");
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
    let (url, _, _) = serve();
    let dir = tempfile::tempdir().unwrap();
    let client = client(&url, Some(dir.path()));
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
    let (url, _, _) = serve();
    let dir = tempfile::tempdir().unwrap();
    let client = client(&url, Some(dir.path()));
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
    let (url, _, _) = serve();
    let dir = tempfile::tempdir().unwrap();
    let client = client(&url, Some(dir.path()));
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

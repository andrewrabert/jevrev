use anyhow::{Context, anyhow, bail};
use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use redb::{ConcurrencyMode, Database, ReadableDatabase, ReadableTable, TableDefinition};
use sha2::{Digest, Sha256};

use crate::Error;
use crate::jev::api::{AskRequest, NoulAnswers, Probability, Questions};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CacheFile(PathBuf);

impl CacheFile {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<CacheFile, Error> {
        let value = value.as_ref();
        if value.is_empty() {
            bail!("the cache file is empty");
        }
        Ok(CacheFile(PathBuf::from(value)))
    }

    pub fn path(&self) -> &Path {
        &self.0
    }
}

const ANSWERS: TableDefinition<&[u8], &[u8]> = TableDefinition::new("ask_response_probabilities");

#[derive(Clone)]
pub struct Cache {
    db: Arc<Database>,
    writer: Arc<Mutex<Option<Writer>>>,
}

struct Writer {
    queue: Sender<Entry>,
    thread: JoinHandle<Result<(), Error>>,
}

struct Entry {
    key: [u8; 32],
    answers: Vec<u8>,
}

pub(crate) struct CacheKey<'a> {
    hash: [u8; 32],
    questions: &'a Questions,
}

impl<'a> CacheKey<'a> {
    pub(crate) fn new(request: &AskRequest, questions: &'a Questions) -> CacheKey<'a> {
        let mut hasher = Sha256::new();
        hasher.update(request.url.as_str());
        hasher.update([0]);
        hasher.update(&request.body);
        CacheKey {
            hash: hasher.finalize().into(),
            questions,
        }
    }
}

impl Cache {
    pub fn open(file: &CacheFile) -> Result<Cache, Error> {
        let path = file.path();
        let open = || -> Result<Database, Error> {
            if let Some(parent) = path.parent() {
                std::fs::create_dir_all(parent)?;
            }
            let mut builder = Database::builder();
            builder.set_concurrency_mode(ConcurrencyMode::MultiWriter);
            let db = builder.create(path)?;
            match db.begin_read()?.open_table(ANSWERS) {
                Ok(_) | Err(redb::TableError::TableDoesNotExist(_)) => {}
                Err(e) => return Err(e.into()),
            }
            Ok(db)
        };
        let db = Arc::new(open().with_context(|| format!("{}", path.display()))?);
        let (queue, received) = std::sync::mpsc::channel();
        let thread = {
            let db = Arc::clone(&db);
            std::thread::spawn(move || write(&db, &received))
        };
        Ok(Cache {
            db,
            writer: Arc::new(Mutex::new(Some(Writer { queue, thread }))),
        })
    }

    pub(crate) async fn get(&self, key: &CacheKey<'_>) -> Result<Option<NoulAnswers>, Error> {
        let hash = key.hash;
        let db = Arc::clone(&self.db);
        let stored = tokio::task::spawn_blocking(move || -> Result<Option<Vec<u8>>, Error> {
            let read = db.begin_read()?;
            let table = match read.open_table(ANSWERS) {
                Ok(table) => table,
                Err(redb::TableError::TableDoesNotExist(_)) => return Ok(None),
                Err(e) => return Err(e.into()),
            };
            Ok(table
                .get(hash.as_slice())?
                .map(|answers| answers.value().to_vec()))
        })
        .await
        .context("the cache read")?
        .context("the cache read")?;
        let Some(stored) = stored else {
            return Ok(None);
        };
        let probabilities = match postcard::from_bytes::<Vec<Probability>>(&stored) {
            Ok(probabilities) => probabilities,
            Err(e) => {
                tracing::debug!("cached answers ignored: {e:#}");
                return Ok(None);
            }
        };
        NoulAnswers::from_cached(probabilities, key.questions)
            .map(Some)
            .context("the cache read")
    }

    pub(crate) fn put(&self, key: &CacheKey<'_>, answers: &NoulAnswers) {
        let entry = Entry {
            key: key.hash,
            answers: postcard::to_stdvec(&answers.probabilities()).expect("probabilities encode"),
        };
        let writer = self
            .writer
            .lock()
            .expect("the cache writer lock is not poisoned");
        if let Some(writer) = writer.as_ref() {
            let _ = writer.queue.send(entry);
        }
    }

    pub fn finish(&self) -> Result<(), Error> {
        let writer = self
            .writer
            .lock()
            .expect("the cache writer lock is not poisoned")
            .take();
        let Some(Writer { queue, thread }) = writer else {
            return Ok(());
        };
        drop(queue);
        thread
            .join()
            .map_err(|_| anyhow!("the cache writer panicked"))?
            .context("the cache write")
    }
}

fn write(db: &Database, received: &Receiver<Entry>) -> Result<(), Error> {
    while let Ok(first) = received.recv() {
        let batch: Vec<Entry> = std::iter::once(first).chain(received.try_iter()).collect();
        let transaction = db.begin_write()?;
        {
            let mut table = transaction.open_table(ANSWERS)?;
            for entry in &batch {
                table.insert(entry.key.as_slice(), entry.answers.as_slice())?;
            }
        }
        transaction.commit()?;
    }
    Ok(())
}

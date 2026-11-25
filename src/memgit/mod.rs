mod fetch;
mod objects;
mod pack;

use std::sync::Arc;

pub use fetch::{FetchError, Remote};
pub use objects::{Objects, ReadError};
pub use pack::{PackError, pack};

#[derive(Debug, Clone)]
pub struct Pack(Arc<[u8]>);

impl Pack {
    pub fn as_bytes(&self) -> &[u8] {
        &self.0
    }

    fn shared(&self) -> Arc<[u8]> {
        self.0.clone()
    }
}

impl From<Vec<u8>> for Pack {
    fn from(bytes: Vec<u8>) -> Pack {
        Pack(bytes.into())
    }
}

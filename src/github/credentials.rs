use anyhow::{anyhow, bail};
use std::ffi::OsStr;

use crate::Error;

#[derive(Clone)]
pub struct Token(String);

impl std::fmt::Debug for Token {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Token(..)")
    }
}

impl Token {
    pub fn try_new(value: impl AsRef<OsStr>) -> Result<Token, Error> {
        let value = value.as_ref();
        let text = value
            .to_str()
            .ok_or_else(|| anyhow!("the GitHub token is not UTF-8"))?;
        if text.is_empty() {
            bail!("the GitHub token is empty");
        }
        Ok(Token(text.to_string()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

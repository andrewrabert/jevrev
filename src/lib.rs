pub mod cli;
pub mod disk;
pub mod files;
pub mod git;
pub mod github;
pub mod gitignore;
pub mod jev;
pub mod languages;
pub mod lines;
pub mod non_empty;
pub mod output;
pub mod rules;
pub mod scan;
pub mod source_path;
pub mod state;
pub mod store;
pub mod templates;

pub use anyhow::Error;

pub const APP_NAME: &str = env!("CARGO_CRATE_NAME");
pub const APP_VERSION: &str = concat!(env!("CARGO_PKG_VERSION"), env!("VERSION_SUFFIX"));

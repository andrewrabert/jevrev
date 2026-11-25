use anyhow::{Context, anyhow, bail};
use std::collections::HashMap;
use std::io::Write;
use std::num::NonZeroU64;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;

use clap::Parser;
use clap::builder::{OsStringValueParser, TypedValueParser};

use crate::disk::{Disk, IgnoreFiles};
use crate::files;
use crate::git::{
    Commits as _, DiffBase, DiffRange, GitDiff, GitDiffSpec, GitDiffToDisk, GitFiles, GitObjectId,
    GitRepo, GitRev, GitRevision, LocalSelection, PackedCommits, ResolvedDiff,
};
use crate::github::credentials::Token;
use crate::github::pull_request::GithubPullRequest;
use crate::github::repo::{GithubHost, GithubRepo, GithubRepoName};
use crate::github::{api, fetch};
use crate::jev::{
    self,
    api::{ApiKey, BaseUrl, Model, Timeout},
    cache::{Cache, CacheFile},
};
use crate::memgit::{Objects, Pack};
use crate::scan::{self, Changes, Concurrency, ScanError, ScanScope};
use crate::source_path::SourcePath;
use crate::store::{self, Finding, GithubSelection, Judgment, Review, Source, Stored, Verdict};
use crate::{Error, output};

const EXIT_CLEAN: u8 = 0;
const EXIT_FINDINGS: u8 = 1;
const EXIT_ERROR: u8 = 2;

const ENV_FILE: &str = "JEVREV_ENV_FILE";
const RULES: &str = "JEVREV_RULES";
const API_KEY: &str = "TYPESAFE_API_KEY";
const BASE_URL: &str = "TYPESAFE_BASE_URL";
const MODEL: &str = "TYPESAFE_DEFAULT_MODEL";
const TIMEOUT: &str = "TYPESAFE_TIMEOUT";
const CONCURRENCY: &str = "JEVREV_CONCURRENCY";
const CACHE: &str = "JEVREV_CACHE";
const GITHUB_HOST: &str = "GITHUB_HOST";
const GITHUB_TOKEN: &str = "GITHUB_TOKEN";

fn after_help() -> String {
    format!(
        "\
REVIEW FLOW:
  1. Scan into a new review file       jevrev -r FILE scan|scan-git|scan-github ...
  2. List findings without a verdict   jevrev -r FILE pending
  3. Confirm or reject each finding    jevrev -r FILE confirm|reject INDEX REASON
  4. Print the review                  jevrev -r FILE show
     or post it on the pull request    jevrev -r FILE submit-github

Run `jevrev COMMAND --help` for each command's options.

ENVIRONMENT:
  Every [env: NAME] a flag shows is read when the flag is not given. --env-file or
  {ENV_FILE} names a dotenv file of those variables; a variable the environment sets
  wins over the file. A missing, unreadable or malformed file is an error.

EXIT STATUS:
  0  No findings, or --exit-zero
  1  Findings
  2  Any error"
    )
}

const PENDING_FIELDS: &str = "\
FIELDS OF EACH --format json LINE:
  index        the finding's INDEX for confirm and reject, from 1
  file         the finding's file, relative to the scanned source
  start, end   the finding's first and last lines
  rule         the rule's id: its rule set's file name and its position in it, from 1,
               such as text.yaml#1
  probability  how likely jev judges the rule broken, 0 to 1
  flags        what the rule flags, as its rule set states it
  violation    the rule's examples of what breaks it
  accepted     the rule's examples of what keeps it
  state        what jev was given of the file: \"file\", the whole file; \"hunk\", one
               hunk of a change; \"hunks\", all of a file's changed hunks; or
               \"language\", one range of the file that a language yields";

const JUDGE_NOTES: &str = "\
A finding's verdict cannot be changed. A command holds the review file's lock while it
runs, and any other command on the file fails at once, so run confirm and reject one at
a time.";

const SUBMIT_NOTES: &str = "\
Findings in files the pull request changes become comments on their lines; the others
are listed in the review's body. The review requests changes, except on a pull request
by the token's own user, where it is a comment. With no confirmed findings, it posts
nothing. It fails when the review is not of a pull request, such as one of a --rev or
--diff scan.";

#[derive(Debug, clap::Parser)]
#[command(
    name = crate::APP_NAME,
    version = crate::APP_VERSION,
    about = "Run jev over a directory or a GitHub pull request and print its findings, confirm or reject each of them, and print the review or post it on the pull request. Exits 0 with no findings, 1 with findings, and 2 on any error.",
    after_help = after_help()
)]
struct Cli {
    /// redb file holding one review: its source, the version of it scanned, and its findings, with their verdicts in a table of their own; a scan given it creates FILE, which must not exist (checked before any request), and leaves no FILE it created when the scan or its save fails; every other command requires it
    #[arg(short, long, value_name = "FILE")]
    review: Option<PathBuf>,
    #[arg(long, value_name = "PATH", global = true, help = format!("Dotenv file of the variables the flags read [env: {ENV_FILE}]"))]
    env_file: Option<PathBuf>,
    /// Least severe level of the log written to stderr
    #[arg(long, value_name = "LEVEL", global = true, default_value = "warn")]
    log_level: LogLevel,
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, clap::Subcommand)]
enum Command {
    /// Run jev over a file, or a directory's files, on disk, whether or not it is in a git repository; print its findings and summary, and save them to a new review file given --review
    Scan {
        /// File or directory to scan; in a directory only regular files are scanned, symlinks not followed; findings are relative to the directory, or the file's own; a file is scanned even when an ignore file matches it
        #[arg(value_name = "PATH")]
        path: PathBuf,
        /// Leave out every entry .ignore files match, and inside a git repository every entry its .gitignore files, .git/info/exclude or the global excludes file match [default]; the last of --ignore and --no-ignore wins
        #[arg(long, overrides_with = "no_ignore")]
        ignore: bool,
        /// Leave nothing out for an ignore file
        #[arg(long, overrides_with = "ignore")]
        no_ignore: bool,
        #[command(flatten)]
        scan: ScanArgs,
    },
    /// Run jev over a git directory at a revision, or over a diff; print its findings and summary, and save them to a new review file given --review
    #[command(
        group(clap::ArgGroup::new("selection").required(true).multiple(false).args(["rev", "diff"]))
    )]
    ScanGit {
        /// Directory in a git repository to scan; findings are relative to it
        #[arg(value_name = "DIR")]
        dir: PathBuf,
        /// Scan the files at REV; conflicts with --diff; one of the two is required
        #[arg(long, value_name = "REV", value_parser = GitRevision::parse)]
        rev: Option<GitRevision>,
        /// Scan a diff, in git diff's syntax: REV, the changes from REV to the files on disk; A..B, from A to B; A...B, from merge-base(A, B) to B; an empty side of A..B or A...B is HEAD
        #[arg(long, value_name = "DIFF", value_parser = GitDiffSpec::parse)]
        diff: Option<GitDiffSpec>,
        #[command(flatten)]
        scan: ScanArgs,
    },
    #[command(
        group(clap::ArgGroup::new("selection").multiple(false).args(["rev", "diff", "pr"]))
    )]
    ScanGithub {
        /// GitHub repo to scan; findings are relative to its top
        #[arg(value_name = "ORG/NAME", value_parser = OsStringValueParser::new().try_map(GithubRepoName::parse))]
        github: GithubRepoName,
        #[arg(long, value_parser = OsStringValueParser::new().try_map(GithubHost::try_new), help = format!("GitHub host [default: github.com] [env: {GITHUB_HOST}]"))]
        github_host: Option<GithubHost>,
        #[arg(long, value_parser = OsStringValueParser::new().try_map(Token::try_new), help = format!("GitHub token [env: {GITHUB_TOKEN}]"))]
        github_token: Option<Token>,
        /// Scan the files at REV [default: the head of the default branch]; conflicts with --diff and --pr
        #[arg(long, value_name = "REV", value_parser = GitRevision::parse)]
        rev: Option<GitRevision>,
        /// Scan a diff, in git diff's syntax: A..B, the changes from A to B; A...B, from merge-base(A, B) to B; an empty side is HEAD; REV alone is rejected before any request; conflicts with --rev and --pr
        #[arg(long, value_name = "A..B", value_parser = GitDiffSpec::parse)]
        diff: Option<GitDiffSpec>,
        /// Scan pull request N's changes; conflicts with --rev and --diff
        #[arg(long, value_name = "N", value_parser = parse_positive)]
        pr: Option<NonZeroU64>,
        #[command(flatten)]
        scan: ScanArgs,
    },
    /// Print the findings without a verdict
    #[command(after_help = PENDING_FIELDS)]
    Pending {
        /// Print a listing, or each finding as a line of JSON [default: text]
        #[arg(long, value_enum, default_value_t = PendingFormat::Text, hide_default_value = true)]
        format: PendingFormat,
        /// Print nothing when no finding is pending
        #[arg(short, long)]
        quiet: bool,
    },
    /// Confirm one finding without a verdict
    #[command(after_help = JUDGE_NOTES)]
    Confirm {
        /// Index of the finding in the scan's findings, from 1
        #[arg(value_name = "INDEX", value_parser = parse_positive)]
        index: NonZeroU64,
        /// One sentence on how the lines match the violation
        #[arg(value_name = "REASON")]
        reason: String,
    },
    /// Reject one finding without a verdict
    #[command(after_help = JUDGE_NOTES)]
    Reject {
        /// Index of the finding in the scan's findings, from 1
        #[arg(value_name = "INDEX", value_parser = parse_positive)]
        index: NonZeroU64,
        /// One sentence on why the lines do not break the rule
        #[arg(value_name = "REASON")]
        reason: String,
    },
    /// Print the review once every finding has a verdict; fails while any finding has none
    Show {
        /// Print the review as JSON, or as markdown of its confirmed findings [default: json]
        #[arg(long, value_enum, default_value_t = Format::Json, hide_default_value = true)]
        format: Format,
    },
    /// Submit the confirmed findings as a review on the GitHub pull request and print its URL, once every finding has a verdict; fails while any finding has none
    #[command(after_help = SUBMIT_NOTES)]
    SubmitGithub {
        #[arg(long, value_parser = OsStringValueParser::new().try_map(Token::try_new), help = format!("GitHub token [env: {GITHUB_TOKEN}]"))]
        github_token: Option<Token>,
    },
}

#[derive(Debug, clap::Args)]
struct ScanArgs {
    #[arg(
        long,
        value_name = "DIR",
        action = clap::ArgAction::Append,
        help = format!("Directory of rule sets jev-scan reviews against; repeat for more directories; replaces {RULES}, a list of directories in the system's path-list syntax [env: {RULES}]")
    )]
    rules: Vec<PathBuf>,
    #[arg(long, value_parser = OsStringValueParser::new().try_map(ApiKey::try_new), help = format!("TypeSafe API key [env: {API_KEY}]"))]
    api_key: Option<ApiKey>,
    #[arg(long, value_parser = OsStringValueParser::new().try_map(BaseUrl::try_new), help = format!("TypeSafe API base URL, an absolute URL [default: https://api.typesafe.ai] [env: {BASE_URL}]"))]
    base_url: Option<BaseUrl>,
    #[arg(long, value_parser = OsStringValueParser::new().try_map(Model::try_new), help = format!("Jev model to ask [default: jev-latest] [env: {MODEL}]"))]
    model: Option<Model>,
    #[arg(long, value_parser = OsStringValueParser::new().try_map(Timeout::try_new), help = format!("Seconds each connection and read to the TypeSafe API may take; inf for no limit [default: 90] [env: {TIMEOUT}]"))]
    timeout: Option<Timeout>,
    #[arg(short, long, value_name = "N", value_parser = OsStringValueParser::new().try_map(Concurrency::try_new), help = format!("Most jev requests in flight at once [default: {}] [env: {CONCURRENCY}]", Concurrency::default()))]
    concurrency: Option<Concurrency>,
    #[arg(long, value_name = "FILE", value_parser = OsStringValueParser::new().try_map(CacheFile::try_new), help = format!("Database file of the jev response cache, created as needed; without it, nothing is cached [env: {CACHE}]"))]
    cache: Option<CacheFile>,
    /// Scan only files PATTERN matches, in gitignore syntax relative to the source; repeat for more patterns, any of which may match
    #[arg(short = 'i', long, value_name = "PATTERN", action = clap::ArgAction::Append)]
    include: Vec<String>,
    /// Leave out files PATTERN matches, in gitignore syntax relative to the source; repeat for more patterns; wins over --include
    #[arg(short = 'e', long, value_name = "PATTERN", action = clap::ArgAction::Append)]
    exclude: Vec<String>,
    /// Exit 0 even when the scan has findings
    #[arg(long)]
    exit_zero: bool,
    /// Print the findings and nothing else
    #[arg(short, long)]
    quiet: bool,
}

struct Scan<'a> {
    review: Option<&'a Path>,
    rules: Vec<PathBuf>,
    client: jev::api::Client,
    concurrency: Concurrency,
    include: Vec<String>,
    exclude: Vec<String>,
    exit_zero: bool,
    quiet: bool,
}

impl ScanArgs {
    fn resolve<'a>(
        self,
        review: Option<&'a Path>,
        environment: &Environment,
    ) -> Result<Scan<'a>, Error> {
        ensure_new_review(review)?;
        let rules: Vec<PathBuf> = if self.rules.is_empty() {
            environment.rules.clone().unwrap_or_default()
        } else {
            self.rules
        }
        .into_iter()
        .filter(|path| !path.as_os_str().is_empty())
        .collect();
        if rules.is_empty() {
            bail!("Give --rules or set {RULES}.");
        }
        let client = jev::api::Client::new(
            self.api_key
                .or_else(|| environment.api_key.clone())
                .ok_or_else(|| anyhow!("Give --api-key or set {API_KEY}."))?,
            &self
                .base_url
                .or_else(|| environment.base_url.clone())
                .unwrap_or_default(),
            self.model
                .or_else(|| environment.model.clone())
                .unwrap_or_default(),
            self.timeout.or(environment.timeout).unwrap_or_default(),
            self.cache
                .or_else(|| environment.cache.clone())
                .map(|file| Cache::open(&file))
                .transpose()?,
        )?;
        Ok(Scan {
            review,
            rules,
            client,
            concurrency: self
                .concurrency
                .or(environment.concurrency)
                .unwrap_or_default(),
            include: self.include,
            exclude: self.exclude,
            exit_zero: self.exit_zero,
            quiet: self.quiet,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
enum LogLevel {
    Error,
    Warn,
    Info,
    Debug,
    Trace,
}

impl From<LogLevel> for tracing::Level {
    fn from(level: LogLevel) -> tracing::Level {
        match level {
            LogLevel::Error => tracing::Level::ERROR,
            LogLevel::Warn => tracing::Level::WARN,
            LogLevel::Info => tracing::Level::INFO,
            LogLevel::Debug => tracing::Level::DEBUG,
            LogLevel::Trace => tracing::Level::TRACE,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
enum PendingFormat {
    Text,
    Json,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
enum Format {
    Json,
    Md,
}

fn parse_positive(value: &str) -> Result<NonZeroU64, String> {
    let number: u64 = value
        .parse()
        .map_err(|e: std::num::ParseIntError| e.to_string())?;
    NonZeroU64::new(number).ok_or_else(|| format!("{value} is not positive"))
}

fn shown(path: &Path) -> String {
    path.display().to_string()
}

fn review_path<'a>(file: Option<&'a Path>, command: &str) -> Result<&'a Path, Error> {
    file.ok_or_else(|| anyhow!("{command}: give --review"))
}

fn with_review<T>(
    path: &Path,
    run: impl FnOnce(&store::Store, Review) -> Result<T, Error>,
) -> Result<T, Error> {
    if !path.is_file() {
        bail!("{}: no such review file", shown(path));
    }
    store::open(path, |store| match store.load()? {
        Stored::Empty => Err(anyhow!("The review file holds no review.")),
        Stored::Review(review) => run(store, review),
    })
}

fn judged_review(file: Option<&Path>, command: &str) -> Result<Review, Error> {
    let review = with_review(review_path(file, command)?, |_, review| Ok(review))?;
    ensure_judged(&review)?;
    Ok(review)
}

fn ensure_judged(review: &Review) -> Result<(), Error> {
    let unjudged: Vec<String> = review
        .findings
        .iter()
        .filter(|f| f.judgment.is_none())
        .map(|f| f.index.to_string())
        .collect();
    if !unjudged.is_empty() {
        bail!("Findings without a verdict: {}", unjudged.join(", "));
    }
    Ok(())
}

/// The variables jevrev reads; one the process environment sets wins over the env file's.
struct Environment {
    rules: Option<Vec<PathBuf>>,
    api_key: Option<ApiKey>,
    base_url: Option<BaseUrl>,
    model: Option<Model>,
    timeout: Option<Timeout>,
    concurrency: Option<Concurrency>,
    cache: Option<CacheFile>,
    github_host: Option<GithubHost>,
    github_token: Option<Token>,
}

impl Environment {
    /// The env file is `env_file`, else the one ENV_FILE names, if either is set.
    fn read(env_file: Option<&Path>) -> Result<Environment, Error> {
        let named = std::env::var_os(ENV_FILE).map(PathBuf::from);
        let mut file: HashMap<String, String> = HashMap::new();
        if let Some(path) = env_file.or(named.as_deref()) {
            let mut read = || -> Result<(), dotenvy::Error> {
                for variable in dotenvy::from_path_iter(path)? {
                    let (name, value) = variable?;
                    file.insert(name, value);
                }
                Ok(())
            };
            read().with_context(|| shown(path))?;
        }
        Ok(Environment {
            rules: match std::env::var_os(RULES) {
                Some(value) => Some(std::env::split_paths(&value).collect()),
                None => file
                    .get(RULES)
                    .map(|value| std::env::split_paths(value).collect()),
            },
            api_key: match std::env::var_os(API_KEY) {
                Some(value) => Some(ApiKey::try_new(value)),
                None => file.remove(API_KEY).map(ApiKey::try_new),
            }
            .transpose()
            .context(API_KEY)?,
            base_url: match std::env::var_os(BASE_URL) {
                Some(value) => Some(BaseUrl::try_new(value)),
                None => file.remove(BASE_URL).map(BaseUrl::try_new),
            }
            .transpose()
            .context(BASE_URL)?,
            model: match std::env::var_os(MODEL) {
                Some(value) => Some(Model::try_new(value)),
                None => file.remove(MODEL).map(Model::try_new),
            }
            .transpose()
            .context(MODEL)?,
            timeout: match std::env::var_os(TIMEOUT) {
                Some(value) => Some(Timeout::try_new(value)),
                None => file.remove(TIMEOUT).map(Timeout::try_new),
            }
            .transpose()
            .context(TIMEOUT)?,
            concurrency: match std::env::var_os(CONCURRENCY) {
                Some(value) => Some(Concurrency::try_new(value)),
                None => file.remove(CONCURRENCY).map(Concurrency::try_new),
            }
            .transpose()
            .context(CONCURRENCY)?,
            cache: match std::env::var_os(CACHE) {
                Some(value) => Some(CacheFile::try_new(value)),
                None => file.remove(CACHE).map(CacheFile::try_new),
            }
            .transpose()
            .context(CACHE)?,
            github_host: match std::env::var_os(GITHUB_HOST) {
                Some(value) => Some(GithubHost::try_new(value)),
                None => file.remove(GITHUB_HOST).map(GithubHost::try_new),
            }
            .transpose()
            .context(GITHUB_HOST)?,
            github_token: match std::env::var_os(GITHUB_TOKEN) {
                Some(value) => Some(Token::try_new(value)),
                None => file.remove(GITHUB_TOKEN).map(Token::try_new),
            }
            .transpose()
            .context(GITHUB_TOKEN)?,
        })
    }
}

/// A scan's review file must not exist yet, checked before any request.
fn ensure_new_review(file: Option<&Path>) -> Result<(), Error> {
    if let Some(file) = file
        && (file.exists() || std::fs::symlink_metadata(file).is_ok())
    {
        bail!("{} already exists", shown(file));
    }
    Ok(())
}

fn utf8_root(path: &Path, root: &Path) -> Result<String, Error> {
    root.to_str()
        .map(String::from)
        .ok_or_else(|| anyhow!("{}: the path is not valid UTF-8", shown(path)))
}

async fn run_jev_scan(
    scan: &Scan<'_>,
    files: &impl files::Source,
    scope: &ScanScope,
    changes: Option<&Changes>,
    name: &str,
) -> Result<(Vec<scan::Finding>, String), Error> {
    let run = async {
        let scanned = scan::scan(&scan.rules, files, scope, changes).await?;
        let reviewed = scan::review(&scanned, &scan.client, scan.concurrency).await?;
        Ok::<_, ScanError>((scanned, reviewed))
    };
    let (scanned, reviewed) = match run.await {
        Err(ScanError::Jev(text)) => bail!("jev-scan: {text}"),
        Err(ScanError::Source(error)) => return Err(error),
        Ok(done) => done,
    };
    scan.client.finish()?;
    if !reviewed.failures.is_empty() {
        bail!(
            "jev-scan: {} jev requests failed:\n{}",
            reviewed.failures.len(),
            reviewed.failures.join("\n")
        );
    }
    let summary = format!(
        "{} states reviewed in {name}, {} findings, {} failures",
        scanned.states.len(),
        reviewed.findings.len(),
        reviewed.failures.len()
    );
    Ok((reviewed.findings, summary))
}

fn save_new(path: &Path, review: &Review, pack: Option<&[u8]>) -> Result<(), Error> {
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| match e.kind() {
            std::io::ErrorKind::AlreadyExists => {
                anyhow!("{} already exists", shown(path))
            }
            _ => anyhow!("{}: {e}", shown(path)),
        })?;
    let saved = store::open(path, |store| {
        store.save(review)?;
        pack.map_or(Ok(()), |pack| store.save_pack(pack))
    });
    saved.map_err(|error| match std::fs::remove_file(path) {
        Ok(()) => error,
        Err(e) => error.context(format!(
            "the save failed, and removing the partial {} failed too: {e}",
            shown(path)
        )),
    })
}

async fn scan_and_save(
    scan: Scan<'_>,
    source: Source,
    files: &impl files::Source,
    changes: Option<&Changes>,
    pack: Option<&[u8]>,
    name: &str,
) -> Result<u8, Error> {
    let scope = ScanScope {
        include: scan.include.clone(),
        exclude: scan.exclude.clone(),
    };
    let (found, summary) = run_jev_scan(&scan, files, &scope, changes, name).await?;
    let review = Review {
        source,
        include: scan.include,
        exclude: scan.exclude,
        findings: found
            .into_iter()
            .enumerate()
            .map(|(n, finding)| Finding::new(finding, n as i64 + 1))
            .collect(),
    };
    if let Some(file) = scan.review {
        save_new(file, &review, pack)?;
    }
    anstream::print!("{}", output::findings_listing(&review.findings));
    if !scan.quiet {
        if !review.findings.is_empty() {
            anstream::println!();
        }
        anstream::println!("{summary}");
    }
    Ok(if !review.findings.is_empty() && !scan.exit_zero {
        EXIT_FINDINGS
    } else {
        EXIT_CLEAN
    })
}

async fn cmd_scan(scan: Scan<'_>, path: &Path, no_ignore: bool) -> Result<u8, Error> {
    let (name, files) = if path.is_dir() {
        let root = std::fs::canonicalize(path).with_context(|| shown(path))?;
        let ignore = if no_ignore {
            IgnoreFiles::Off
        } else {
            IgnoreFiles::Apply
        };
        (utf8_root(path, &root)?, Disk::Directory { root, ignore })
    } else if path.is_file() {
        let absolute = std::path::absolute(path)?;
        let (Some(parent), Some(file_name)) = (absolute.parent(), absolute.file_name()) else {
            bail!("{}: not a file path", shown(path));
        };
        let directory = std::fs::canonicalize(parent).with_context(|| shown(path))?;
        let root = directory.join(file_name);
        let name = SourcePath::from_base(&directory, &root).with_context(|| shown(path))?;
        (
            utf8_root(path, &root)?,
            Disk::File {
                path: root.clone(),
                name,
            },
        )
    } else {
        bail!("{}: no such file or directory", shown(path));
    };
    scan_and_save(
        scan,
        Source::LocalDir { path: name.clone() },
        &files,
        None,
        None,
        &name,
    )
    .await
}

async fn cmd_scan_git(
    scan: Scan<'_>,
    dir: &Path,
    rev: Option<&GitRevision>,
    diff: Option<&GitDiffSpec>,
) -> Result<u8, Error> {
    let root = std::fs::canonicalize(dir).with_context(|| shown(dir))?;
    if !root.is_dir() {
        bail!("{} is not a directory", shown(dir));
    }
    let name = utf8_root(dir, &root)?;
    let Ok(repo) = GitRepo::open(&root) else {
        bail!("{name} is not in a git repository");
    };
    let (selection, commits, id, changes) = match (rev, diff) {
        (Some(rev), _) => {
            let rev = repo.resolve_rev(rev)?;
            let commits = repo.pack(&[&rev.commit])?;
            let id = rev.commit.clone();
            (LocalSelection::Rev(rev), commits, id, None)
        }
        (None, Some(spec)) => match repo.resolve_diff(spec)? {
            ResolvedDiff::Commits(diff) => {
                let commits = repo.pack(&[&diff.base, &diff.head])?;
                let changes = Changes {
                    diff: commits.changes_between_commits(
                        &diff.base,
                        &diff.head,
                        scan::DIFF_CONTEXT,
                        0,
                    )?,
                    untracked: Vec::new(),
                };
                let id = diff.head.clone();
                (LocalSelection::Diff(diff), commits, id, Some(changes))
            }
            ResolvedDiff::ToDisk { base } => {
                let (commits, tree, changes) = repo.pack_working_tree(&base, scan::DIFF_CONTEXT)?;
                let selection = LocalSelection::DiffToDisk(GitDiffToDisk {
                    base,
                    tree: tree.clone(),
                });
                (selection, commits, tree, Some(changes))
            }
        },
        (None, None) => bail!("scan-git: give --rev or --diff"),
    };
    let files = GitFiles {
        commits: Arc::new(commits),
        id,
    };
    let pack = files.commits.pack().clone();
    scan_and_save(
        scan,
        Source::LocalGitDir {
            path: name.clone(),
            git: selection,
        },
        &files,
        changes.as_ref(),
        Some(pack.as_bytes()),
        &name,
    )
    .await
}

async fn github_selection(
    pr: Option<NonZeroU64>,
    rev: Option<&GitRevision>,
    diff: Option<&DiffRange>,
    client: &api::Client,
    repo: &GithubRepo,
) -> Result<GithubSelection, Error> {
    if let Some(number) = pr {
        let pull_request = client.pull_request(repo, number).await?;
        return Ok(GithubSelection::PullRequest(GithubPullRequest {
            base: client
                .merge_base(repo, &pull_request.target, &pull_request.head)
                .await?,
            head: pull_request.head,
            number,
        }));
    }
    if let Some(range) = diff {
        let mut base = client.commit(repo, &range.base).await?;
        let head = client.commit(repo, &range.head).await?;
        if range.from == DiffBase::MergeBase {
            base = client.merge_base(repo, &base, &head).await?;
        }
        return Ok(GithubSelection::Diff(GitDiff { base, head }));
    }
    let commit = match rev {
        Some(rev) => client.commit(repo, rev).await?,
        None => client.default_branch_head(repo).await?,
    };
    Ok(GithubSelection::Rev(GitRev { commit }))
}

async fn fetched(
    repo: &GithubRepo,
    token: &Token,
    commits: &[&GitObjectId],
) -> Result<Arc<PackedCommits>, Error> {
    let pack = fetch::fetch(repo, Some(token), commits).await?;
    Ok(Arc::new(PackedCommits::new(Objects::new(pack)?, None)))
}

async fn cmd_scan_github(
    scan: Scan<'_>,
    repo: &GithubRepo,
    github: &api::Client,
    token: &Token,
    rev: Option<&GitRevision>,
    diff: Option<&GitDiffSpec>,
    pr: Option<NonZeroU64>,
) -> Result<u8, Error> {
    let diff = match diff {
        Some(GitDiffSpec::Range(range)) => Some(range),
        Some(GitDiffSpec::ToDisk { .. }) => {
            bail!("--diff: give A..B or A...B; a GitHub repo has no files on disk")
        }
        None => None,
    };
    let selection = github_selection(pr, rev, diff, github, repo).await?;
    let (files, changes) = match &selection {
        GithubSelection::Rev(rev) => {
            let commits = fetched(repo, token, &[&rev.commit]).await?;
            (
                GitFiles {
                    commits,
                    id: rev.commit.clone(),
                },
                None,
            )
        }
        GithubSelection::Diff(GitDiff { base, head })
        | GithubSelection::PullRequest(GithubPullRequest { base, head, .. }) => {
            let commits = fetched(repo, token, &[base, head]).await?;
            let diff = commits.changes_between_commits(base, head, scan::DIFF_CONTEXT, 0)?;
            (
                GitFiles {
                    commits,
                    id: head.clone(),
                },
                Some(Changes {
                    diff,
                    untracked: Vec::new(),
                }),
            )
        }
    };
    let pack = files.commits.pack().clone();
    let name = format!("{}/{}", repo.host(), repo.name());
    scan_and_save(
        scan,
        Source::GithubRepo {
            repo: repo.clone(),
            git: selection,
        },
        &files,
        changes.as_ref(),
        Some(pack.as_bytes()),
        &name,
    )
    .await
}

async fn cmd_submit_github(file: Option<&Path>, token: &Token) -> Result<(), Error> {
    let (review, pack) = with_review(review_path(file, "submit-github")?, |store, review| {
        Ok((review, store.load_pack()?))
    })?;
    ensure_judged(&review)?;
    let Source::GithubRepo {
        repo,
        git: GithubSelection::PullRequest(pr),
    } = &review.source
    else {
        bail!("The review is not of a GitHub pull request.");
    };
    let client = api::Client::new(Some(repo.host().clone()), token)?;
    let diff = PackedCommits::new(Objects::new(Pack::from(pack))?, None).changes_between_commits(
        &pr.base,
        &pr.head,
        api::REVIEW_DIFF_CONTEXT,
        api::REVIEW_INTER_HUNK_CONTEXT,
    )?;
    let url = client.submit_review(repo, pr, &review, &diff).await?;
    writeln!(std::io::stdout(), "{url}")?;
    Ok(())
}

fn cmd_pending(file: Option<&Path>, format: PendingFormat, quiet: bool) -> Result<(), Error> {
    let review = with_review(review_path(file, "pending")?, |_, review| Ok(review))?;
    match format {
        PendingFormat::Text => {
            let pending: Vec<&Finding> = review
                .findings
                .iter()
                .filter(|f| f.judgment.is_none())
                .collect();
            if pending.is_empty() {
                if !quiet {
                    anstream::println!("No findings without a verdict.");
                }
            } else {
                anstream::print!("{}", output::findings_listing(pending));
            }
        }
        PendingFormat::Json => write!(std::io::stdout(), "{}", output::pending_lines(&review))?,
    }
    Ok(())
}

fn judge(
    file: Option<&Path>,
    command: &str,
    index: NonZeroU64,
    reason: &str,
    verdict: Verdict,
) -> Result<(), Error> {
    let index = index.get();
    let path = review_path(file, command)?;
    if !path.is_file() {
        bail!("{}: no such review file", shown(path));
    }
    let judgment = Judgment {
        verdict,
        reason: reason.to_string(),
    };
    store::open(path, |store| Ok(store.judge(index, &judgment)?))?;
    writeln!(std::io::stdout(), "Finding {index} {verdict}")?;
    Ok(())
}

fn cmd_show(file: Option<&Path>, format: Format) -> Result<(), Error> {
    let review = judged_review(file, "show")?;
    let text = match format {
        Format::Json => output::review_json(&review),
        Format::Md => output::review_markdown(&review),
    };
    writeln!(std::io::stdout(), "{text}")?;
    Ok(())
}

async fn run(cli: Cli) -> Result<u8, Error> {
    let environment = Environment::read(cli.env_file.as_deref())?;
    let file = cli.review.as_deref();
    let github_token = |token: Option<Token>| -> Result<Token, Error> {
        token
            .or_else(|| environment.github_token.clone())
            .ok_or_else(|| anyhow!("Give --github-token or set {GITHUB_TOKEN}."))
    };
    match cli.command {
        Command::Scan {
            path,
            no_ignore,
            scan,
            ..
        } => {
            let scan = scan.resolve(file, &environment)?;
            cmd_scan(scan, &path, no_ignore).await
        }
        Command::ScanGit {
            dir,
            rev,
            diff,
            scan,
        } => {
            let scan = scan.resolve(file, &environment)?;
            cmd_scan_git(scan, &dir, rev.as_ref(), diff.as_ref()).await
        }
        Command::ScanGithub {
            github,
            github_host,
            github_token: token,
            rev,
            diff,
            pr,
            scan,
        } => {
            let scan = scan.resolve(file, &environment)?;
            let token = github_token(token)?;
            let client = api::Client::new(
                github_host.or_else(|| environment.github_host.clone()),
                &token,
            )?;
            let repo = GithubRepo::new(client.host().clone(), github);
            cmd_scan_github(
                scan,
                &repo,
                &client,
                &token,
                rev.as_ref(),
                diff.as_ref(),
                pr,
            )
            .await
        }
        Command::Pending { format, quiet } => cmd_pending(file, format, quiet).map(|()| EXIT_CLEAN),
        Command::Confirm { index, reason } => {
            judge(file, "confirm", index, &reason, Verdict::Confirmed).map(|()| EXIT_CLEAN)
        }
        Command::Reject { index, reason } => {
            judge(file, "reject", index, &reason, Verdict::Rejected).map(|()| EXIT_CLEAN)
        }
        Command::Show { format } => cmd_show(file, format).map(|()| EXIT_CLEAN),
        Command::SubmitGithub {
            github_token: token,
        } => cmd_submit_github(file, &github_token(token)?)
            .await
            .map(|()| EXIT_CLEAN),
    }
}

pub async fn main() -> ExitCode {
    let cli = Cli::parse();
    tracing_subscriber::fmt()
        .with_max_level(tracing::Level::from(cli.log_level))
        .with_writer(std::io::stderr)
        .with_target(false)
        .without_time()
        .init();
    if rustls::crypto::ring::default_provider()
        .install_default()
        .is_err()
    {
        eprintln!("error: another TLS crypto provider is already installed");
        return ExitCode::from(EXIT_ERROR);
    }
    match run(cli).await {
        Ok(status) => ExitCode::from(status),
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::from(EXIT_ERROR)
        }
    }
}

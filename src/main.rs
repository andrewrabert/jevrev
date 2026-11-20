#[tokio::main(flavor = "current_thread")]
async fn main() -> std::process::ExitCode {
    jevrev::cli::main().await
}

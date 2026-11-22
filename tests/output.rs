use std::path::Path;

use jevrev::output::{findings_listing, pending_lines, review_json, review_markdown};
use jevrev::store::{Finding, Stored, open};

fn fixtures() -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
}

fn read(path: &Path) -> String {
    String::from_utf8(std::fs::read(path).unwrap()).unwrap()
}

#[test]
fn review_outputs_equal_the_captured_ones() {
    let directory = fixtures().join("review");
    let scratch = tempfile::tempdir().unwrap();
    for name in [
        "github-diff",
        "github-pr",
        "github-rev",
        "local-dir",
        "local-git-diff",
        "local-git-diff-to-disk",
        "local-git-rev",
        "pending",
    ] {
        let copy = scratch.path().join(format!("{name}.json"));
        std::fs::copy(directory.join(format!("{name}.json")), &copy).unwrap();
        let Stored::Review(review) = open(&copy, |store| store.load()).unwrap() else {
            panic!("{name} holds no review");
        };
        let expected =
            |label: &str, part: &str| read(&directory.join(format!("{name}.{label}.{part}")));
        assert_eq!(expected("pending", "status"), "0\n");
        assert_eq!(pending_lines(&review), expected("pending", "out"), "{name}");
        assert_eq!(
            anstream::adapter::strip_str(&findings_listing(
                review.findings.iter().filter(|f| f.judgment.is_none())
            ))
            .to_string(),
            expected("pending-text", "out"),
            "{name}"
        );
        if name == "pending" {
            assert_eq!(expected("show-json", "status"), "2\n");
            assert_eq!(
                expected("show-json", "err"),
                "error: Findings without a verdict: 2, 4\n"
            );
            continue;
        }
        assert_eq!(expected("show-json", "status"), "0\n");
        assert_eq!(
            review_json(&review) + "\n",
            expected("show-json", "out"),
            "{name}"
        );
        assert_eq!(expected("show-md", "status"), "0\n");
        assert_eq!(
            review_markdown(&review) + "\n",
            expected("show-md", "out"),
            "{name}"
        );
    }
}

#[test]
fn the_findings_listing_equals_the_captured_one() {
    let directory = fixtures().join("output");
    let findings: Vec<Finding> =
        serde_json::from_str(&read(&directory.join("findings.json"))).unwrap();
    let listing = findings_listing(&findings);
    assert_eq!(listing, read(&directory.join("findings-colour.txt")));
    assert_eq!(
        anstream::adapter::strip_str(&listing).to_string(),
        read(&directory.join("findings-plain.txt"))
    );
}

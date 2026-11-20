use jevrev::git::{DiffBase, DiffRange, GitDiffSpec, GitObjectId, GitRevision};

fn spec(base: &str, head: Option<&str>, from: DiffBase) -> GitDiffSpec {
    let base = GitRevision::parse(base).unwrap();
    match head {
        None => GitDiffSpec::ToDisk { base },
        Some(head) => GitDiffSpec::Range(DiffRange {
            base,
            head: GitRevision::parse(head).unwrap(),
            from,
        }),
    }
}

#[test]
fn diff_specs_parse_in_every_form() {
    use DiffBase::{Base, MergeBase};
    let cases = [
        ("main", spec("main", None, Base)),
        ("a..b", spec("a", Some("b"), Base)),
        ("a...b", spec("a", Some("b"), MergeBase)),
        ("..b", spec("HEAD", Some("b"), Base)),
        ("a..", spec("a", Some("HEAD"), Base)),
        ("...b", spec("HEAD", Some("b"), MergeBase)),
        ("a...", spec("a", Some("HEAD"), MergeBase)),
        ("a..b...c", spec("a..b", Some("c"), MergeBase)),
    ];
    for (text, expected) in cases {
        assert_eq!(GitDiffSpec::parse(text).unwrap(), expected, "{text}");
    }
    for text in ["..", "...", "a..b..c", "a...b...c", "-x", "a b"] {
        assert_eq!(
            GitDiffSpec::parse(text).unwrap_err().to_string(),
            format!("\"{text}\" is not a diff")
        );
    }
    assert_eq!(
        GitRevision::parse("-x").unwrap_err().to_string(),
        "\"-x\" is not a revision"
    );
    assert!(GitObjectId::parse("0123456789abcdef0123456789abcdef01234567").is_ok());
    assert_eq!(
        GitObjectId::parse("ABC").unwrap_err().to_string(),
        "\"ABC\" is not an object id"
    );
}

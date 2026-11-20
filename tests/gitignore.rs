use jevrev::gitignore::{Check, Pattern, Spec};
use jevrev::source_path::SourcePath;

type Case = (&'static [&'static str], &'static [(&'static str, Check)]);

#[test]
fn patterns_match_as_git_does() {
    let cases: &[Case] = &[
        (
            &["*.py"],
            &[
                ("a.py", Check::Included),
                ("dir/a.py", Check::Included),
                ("a.pyc", Check::Unmatched),
                ("a.py/x", Check::Included),
            ],
        ),
        (
            &["/root.txt"],
            &[
                ("root.txt", Check::Included),
                ("sub/root.txt", Check::Unmatched),
            ],
        ),
        (
            &["dir/"],
            &[
                ("dir", Check::Unmatched),
                ("dir/x", Check::Included),
                ("a/dir/x", Check::Included),
                ("dirx/y", Check::Unmatched),
            ],
        ),
        (
            &["**/x"],
            &[
                ("x", Check::Included),
                ("a/x", Check::Included),
                ("a/b/x", Check::Included),
                ("x/y", Check::Included),
            ],
        ),
        (
            &["a/**/b"],
            &[
                ("a/b", Check::Included),
                ("a/x/b", Check::Included),
                ("a/x/y/b", Check::Included),
                ("b", Check::Unmatched),
            ],
        ),
        (
            &["*.log", "!keep.log"],
            &[
                ("a.log", Check::Included),
                ("keep.log", Check::Excluded),
                ("d/keep.log", Check::Excluded),
            ],
        ),
        (
            &["\\#hash"],
            &[("#hash", Check::Included), ("hash", Check::Unmatched)],
        ),
        (
            &["trailing\\ "],
            &[
                ("trailing ", Check::Included),
                ("trailing", Check::Unmatched),
            ],
        ),
        (
            &["trail  "],
            &[("trail", Check::Included), ("trail  ", Check::Unmatched)],
        ),
        (
            &["[a-c]?"],
            &[
                ("ax", Check::Included),
                ("dx", Check::Unmatched),
                ("a", Check::Unmatched),
            ],
        ),
        (
            &["[!a]x"],
            &[("ax", Check::Unmatched), ("bx", Check::Included)],
        ),
        (&["[]]"], &[("]", Check::Included), ("x", Check::Unmatched)]),
        (
            &["[[:alpha:]]"],
            &[
                ("a", Check::Unmatched),
                (":", Check::Unmatched),
                ("[", Check::Unmatched),
                ("]", Check::Unmatched),
            ],
        ),
        (
            &["*/"],
            &[("a", Check::Unmatched), ("a/b", Check::Included)],
        ),
        (
            &["**/"],
            &[("a", Check::Unmatched), ("a/b", Check::Included)],
        ),
        (
            &["dir/", "!dir/keep"],
            &[
                ("dir/keep", Check::Included),
                ("dir/other", Check::Included),
            ],
        ),
        (&["*.txt", "!/dir/"], &[("dir/a.txt", Check::Included)]),
        (
            &["a[--z]b"],
            &[
                ("a-b", Check::Included),
                ("acb", Check::Included),
                ("a.b", Check::Included),
            ],
        ),
        (
            &["x&&y", "a~~b"],
            &[("x&&y", Check::Included), ("a~~b", Check::Included)],
        ),
        (
            &["foo/*"],
            &[
                ("foo/a", Check::Included),
                ("foo/a/b", Check::Included),
                ("foo", Check::Unmatched),
            ],
        ),
        (
            &["doc/**"],
            &[
                ("doc/a", Check::Included),
                ("doc/a/b", Check::Included),
                ("doc", Check::Unmatched),
            ],
        ),
        (&["**"], &[("a", Check::Included), ("a/b", Check::Included)]),
        (
            &["a?c"],
            &[("abc", Check::Included), ("a/c", Check::Unmatched)],
        ),
        (&["/"], &[("a", Check::Unmatched)]),
        (&["#c"], &[("#c", Check::Unmatched)]),
    ];
    for (patterns, expected) in cases {
        let spec = Spec::from_lines(patterns.iter().copied()).unwrap();
        for (path, check) in *expected {
            assert_eq!(
                spec.check(&SourcePath::new(path, None).unwrap()),
                *check,
                "{patterns:?} against {path:?}"
            );
        }
    }
    assert_eq!(Pattern::new("*.py").as_str(), "*.py");
    assert!(Spec::from_lines(["[a-"]).is_err());
}

use std::path::Path;

use jevrev::rules::{Conditions, Language, RuleSet};

type Expected = (
    &'static str,
    usize,
    f64,
    &'static [&'static [Language]],
    usize,
    usize,
);

#[test]
fn every_example_rule_set_loads() {
    let expected: &[Expected] = &[
        (
            "aardwolf-mcp.yaml",
            10,
            0.75,
            &[&[Language::Python]],
            20,
            2394,
        ),
        (
            "jev-pr-review.yaml",
            4,
            0.35,
            &[&[Language::Python]],
            12,
            495,
        ),
        ("justfile.yaml", 1, 0.75, &[&[Language::Just]], 2, 94),
        ("prose.yaml", 1, 0.75, &[&[]], 1, 99),
        (
            "python-construction.yaml",
            18,
            0.75,
            &[&[Language::Python]],
            37,
            5002,
        ),
        ("python.yaml", 47, 0.75, &[&[Language::Python]], 94, 6530),
        ("shell.yaml", 4, 0.75, &[&[Language::Sh]], 8, 376),
        ("standards.yaml", 19, 0.75, &[&[Language::Rust]], 44, 1957),
        ("text.yaml", 3, 0.75, &[&[]], 5, 300),
    ];
    let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("example_rules");
    for (name, rules, threshold, languages, examples, flags) in expected {
        let set = RuleSet::load(&directory.join(name)).unwrap();
        assert_eq!(set.rules.len(), *rules, "{name}");
        assert_eq!(set.error_threshold, *threshold, "{name}");
        let Conditions::Given(when) = &set.when else {
            panic!("{name} has no when");
        };
        let found: Vec<&[Language]> = when.iter().map(|w| w.language.as_slice()).collect();
        assert_eq!(found, *languages, "{name}");
        let found_examples: usize = set.rules.iter().map(|r| r.good.len() + r.bad.len()).sum();
        assert_eq!(found_examples, *examples, "{name}");
        let found_flags: usize = set.rules.iter().map(|r| r.flags.chars().count()).sum();
        assert_eq!(found_flags, *flags, "{name}");
    }
}

#[test]
fn rule_sets_are_validated() {
    let set = RuleSet::parse("error_threshold: 0.5\nrules: []\n").unwrap();
    assert_eq!(set.error_threshold, 0.5);
    assert!(matches!(set.when, Conditions::Absent));
    let set = RuleSet::parse("error_threshold: 1\nwhen: ~\nrules: [{flags: x}]\n").unwrap();
    assert_eq!(set.error_threshold, 1.0);
    assert!(matches!(set.when, Conditions::Absent));
    let set = RuleSet::parse(
        "error_threshold: 0.5\nrules: []\nwhen: [{language: [rust], matches: ['*.rs']}]\n",
    )
    .unwrap();
    let Conditions::Given(when) = &set.when else {
        panic!("no when");
    };
    assert_eq!(when[0].language, [Language::Rust]);
    assert_eq!(when[0].matches.len(), 1);
    assert!(RuleSet::parse("error_threshold: 0.5\nrules: [{good: [a]}]\n").is_err());
    assert!(RuleSet::parse("error_threshold: 2\nrules: []\n").is_err());
    assert!(RuleSet::parse("error_threshold: 0.5\nrules: []\nother: 1\n").is_err());
    assert!(RuleSet::parse("error_threshold: 0.5\nrules: []\nwhen: [{language: [go]}]\n").is_err());
    assert!(RuleSet::parse("error_threshold: 0.5\nrules: [\n").is_err());
}

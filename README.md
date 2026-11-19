# jevrev

A CLI that runs jev over a directory or a GitHub pull request, records a verdict on each finding, and prints the review or posts it on the pull request.

Give the directory of rule sets to review against with `--rules` or `JEV_RULES_DIR`. [example_rules](example_rules) holds examples.

A review is one SQLite file given by `--db`. A pull request is cloned under `--cache-dir` and left there. [cli.md](cli.md) is the CLI's surface.

```sh
jevrev --db review.db scan --repo github.com/ORG/NAME --pr 12 --rules example_rules
jevrev --db review.db pending
jevrev --db review.db confirm 1 "Why the lines break the rule"
jevrev --db review.db reject 2 "Why the lines do not break the rule"
jevrev --db review.db show --format md
jevrev --db review.db post
```

`scan --dir DIR --base REV` scans a git dir's change from REV in place, and `scan --dir DIR --full` scans every file of any directory.

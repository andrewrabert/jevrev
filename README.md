# jev-code-review

A CLI that runs jev over a pull request or two revisions of a local git dir, judges each finding in a read-only [bashkit](https://pypi.org/project/bashkit/) sandbox, and posts the confirmed findings as a review on the pull request.

Give the directory of rule sets to review against with `--rules` or `JEV_RULES_DIR`. [example_rules](example_rules) holds examples.

A review's data is in one SQLite state file given by `--state`. Its clone is kept under `--cache-dir` until the review finishes or is cancelled. [cli.md](cli.md) is the CLI's surface.

```sh
jev-code-review --state review.db start-review --repo github.com/ORG/NAME --pr 12 --rules example_rules
jev-code-review --state review.db sandbox read --file-path /sandbox/jev-findings.jsonl
jev-code-review --state review.db confirm-finding 1 "Why the lines break the rule"
jev-code-review --state review.db finish-review
jev-code-review --state review.db post-review
jev-code-review --state review.db read-review
```

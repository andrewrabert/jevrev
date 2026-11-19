# jevrev

A CLI that runs jev over a local directory, a local git directory or a GitHub repo, records a verdict on each finding, and prints the review or posts it on the pull request.

Give the directory of rule sets to review against with `--rules` or `JEV_RULES_DIR`. [example_rules](example_rules) holds examples.

A review is one SQLite file given by `--db`. No scan writes anything else: a git directory's files are read from its repository, and a GitHub repo's through the GitHub API. [cli.md](cli.md) is the CLI's surface.

```sh
jevrev --db review.db scan-github ORG/NAME --github-pr 12 --rules example_rules
jevrev --db review.db pending
jevrev --db review.db confirm 1 "Why the lines break the rule"
jevrev --db review.db reject 2 "Why the lines do not break the rule"
jevrev --db review.db show --format md
jevrev --db review.db submit-github
```

| command | reviews |
|---|---|
| `scan DIR` | DIR's files on disk |
| `scan-git DIR --git-rev REV` | DIR's files at REV |
| `scan-git DIR --git-diff REV` | the changes from REV to the files on disk, as `git diff REV` shows them |
| `scan-git DIR --git-diff A..B` / `A...B` | the changes from A, or merge-base(A, B), to B |
| `scan-github ORG/NAME` | the files at the head of the default branch |
| `scan-github ORG/NAME --git-rev REV` | the files at REV |
| `scan-github ORG/NAME --git-diff A..B` / `A...B` | the changes from A, or merge-base(A, B), to B |
| `scan-github ORG/NAME --github-pr N` | pull request N's changes |

Every scan leaves out files `.gitignore`, `.ignore`, `.git/info/exclude` or `core.excludesFile` matches, unless given `--no-ignore`.

GitHub is reached on the host `GH_HOST` names, `github.com` when unset, with the token in `GH_TOKEN` or `GITHUB_TOKEN` (`GH_ENTERPRISE_TOKEN` or `GITHUB_ENTERPRISE_TOKEN` on another host), or else the one `gh auth token` prints when `gh` is installed. A review records its host; `submit-github` posts there.

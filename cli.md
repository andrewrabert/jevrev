# Surface: CLI
Each subcommand, flag, option, and positional argument declaration below
exists exactly as written.

## `jevrev`
```
--db <FILE>  SQLite file holding one review: its source, the version of it scanned, metadata, findings, and verdicts

COMMAND:
  scan           Run jev over a directory's files on disk into a new db, whether or not it is in a git repository; print the scan's summary
  scan-git       Run jev over a git directory at a revision, or over a diff, into a new db; print the scan's summary
  scan-github    Run jev over a GitHub repo, through the GitHub API on the host GH_HOST names (github.com when unset), into a new db; print the scan's summary
  pending        Print each finding without a verdict as a line of JSON, its file relative to the source
  confirm        Confirm one finding without a verdict
  reject         Reject one finding without a verdict
  show           Print the review once every finding has a verdict; fails while any finding has none
  submit-github  Submit the confirmed findings as a review on the GitHub pull request and print its URL, once every finding has a verdict; fails while any finding has none
```

## `jevrev scan`
```
DIR                       Directory to scan; findings are relative to it
--rules <DIR>             Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>      More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
--ignore                  Leave out every file .gitignore, .ignore, .git/info/exclude or core.excludesFile matches [default]; the last of --ignore and --no-ignore wins
--no-ignore               Scan files an ignore file matches too
--metadata <KEY> <VALUE>  Metadata stored with the review and printed by show; repeat for more keys
```

## `jevrev scan-git`
```
DIR                       Directory in a git repository to scan; findings are relative to it
--git-rev <REV>           Scan the files at REV; conflicts with --git-diff; one of the two is required
--git-diff <DIFF>         Scan a diff, in git diff's syntax: REV, the changes from REV to the files on disk; A..B, from A to B; A...B, from merge-base(A, B) to B; an empty side of A..B or A...B is HEAD
--rules <DIR>             Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>      More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
--ignore                  Leave out every file .gitignore, .ignore, .git/info/exclude or core.excludesFile matches [default]; the last of --ignore and --no-ignore wins
--no-ignore               Scan files an ignore file matches too
--metadata <KEY> <VALUE>  Metadata stored with the review and printed by show; repeat for more keys
```

## `jevrev scan-github`
```
ORG/NAME                  GitHub repo to scan; findings are relative to its top
--git-rev <REV>           Scan the files at REV [default: the head of the default branch]; conflicts with --git-diff and --github-pr
--git-diff <A..B>         Scan a diff, in git diff's syntax: A..B, the changes from A to B; A...B, from merge-base(A, B) to B; an empty side is HEAD; conflicts with --git-rev and --github-pr
--github-pr <N>           Scan pull request N's changes; conflicts with --git-rev and --git-diff
--rules <DIR>             Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>      More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
--ignore                  Leave out every file .gitignore, .ignore or core.excludesFile matches [default]; the last of --ignore and --no-ignore wins
--no-ignore               Scan files an ignore file matches too
--metadata <KEY> <VALUE>  Metadata stored with the review and printed by show; repeat for more keys
```

## `jevrev confirm`
```
INDEX   Index of the finding in the scan's findings, from 1
REASON  One sentence on how the lines match the violation
```

## `jevrev reject`
```
INDEX   Index of the finding in the scan's findings, from 1
REASON  One sentence on why the lines do not break the rule
```

## `jevrev show`
```
--format <json|md>  Print the review as JSON, or as markdown of its confirmed findings [default: json]
```

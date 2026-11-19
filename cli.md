# Surface: CLI
Each subcommand, flag, option, and positional argument declaration below
exists exactly as written.

## `jevrev`
```
--db <FILE>  SQLite file holding one review: its source, the version of it scanned, metadata, findings, and verdicts; a scan saves its review to it when given, every other command requires it

EXIT STATUS:
  0  No findings, or --exit-zero
  1  Findings
  2  Any error, including an unexpected exception

COMMAND:
  scan           Run jev over a file, or a directory's files, on disk, whether or not it is in a git repository; print its findings and summary, and save them to a new db given --db
  scan-git       Run jev over a git directory at a revision, or over a diff; print its findings and summary, and save them to a new db given --db
  scan-github    Run jev over a GitHub repo, through the GitHub API on the host GH_HOST names (github.com when unset); print its findings and summary, and save them to a new db given --db
  pending        Print each finding without a verdict as a line of JSON, its file relative to the source
  confirm        Confirm one finding without a verdict
  reject         Reject one finding without a verdict
  show           Print the review once every finding has a verdict; fails while any finding has none
  submit-github  Submit the confirmed findings as a review on the GitHub pull request and print its URL, once every finding has a verdict; fails while any finding has none
```

## `jevrev scan`
```
PATH                      File or directory to scan, symlinks followed; findings are relative to the directory, or the file's own; a file is scanned even when an ignore file matches it
--rules <DIR>             Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>      More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
--ignore                  Leave out every file .gitignore, .ignore, .git/info/exclude or core.excludesFile matches [default]; the last of --ignore and --no-ignore wins
--no-ignore               Scan files an ignore file matches too
-i, --include <PATTERN>   Scan only files PATTERN matches, in gitignore syntax relative to the source, of those the ignore files leave; repeat for more patterns, any of which may match
-e, --exclude <PATTERN>   Leave out files PATTERN matches, in gitignore syntax relative to the source; repeat for more patterns; wins over --include
--metadata <KEY> <VALUE>  Metadata stored with the review and printed by show; repeat for more keys; requires --db
--exit-zero               Exit 0 even when the scan has findings
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
-i, --include <PATTERN>   Scan only files PATTERN matches, in gitignore syntax relative to the source, of those the ignore files leave; repeat for more patterns, any of which may match
-e, --exclude <PATTERN>   Leave out files PATTERN matches, in gitignore syntax relative to the source; repeat for more patterns; wins over --include
--metadata <KEY> <VALUE>  Metadata stored with the review and printed by show; repeat for more keys; requires --db
--exit-zero               Exit 0 even when the scan has findings
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
-i, --include <PATTERN>   Scan only files PATTERN matches, in gitignore syntax relative to the source, of those the ignore files leave; repeat for more patterns, any of which may match
-e, --exclude <PATTERN>   Leave out files PATTERN matches, in gitignore syntax relative to the source; repeat for more patterns; wins over --include
--metadata <KEY> <VALUE>  Metadata stored with the review and printed by show; repeat for more keys; requires --db
--exit-zero               Exit 0 even when the scan has findings
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

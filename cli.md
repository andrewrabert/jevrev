# Surface: CLI
Each subcommand, flag, option, and positional argument declaration below
exists exactly as written.

## `jevrev`
```
--db <FILE>  SQLite file holding one review: its dir, base, pull request, metadata, findings, and verdicts

COMMAND:
  scan           Run jev over a directory, or a clone of a pull request, into a new db: the change from a base, or every file when full; print the scan's summary
  pending        Print each finding without a verdict as a line of JSON, its file relative to the dir
  confirm        Confirm one finding without a verdict
  reject         Reject one finding without a verdict
  show           Print the review once every finding has a verdict; fails while any finding has none
  submit-github  Submit the confirmed findings as a review on the GitHub pull request and print its URL, once every finding has a verdict; fails while any finding has none
```

## `jevrev scan`
```
--dir <DIR>               Directory to scan in place; findings are relative to it; requires --base or --full; conflicts with --repo
--repo <HOST/ORG/NAME>    GitHub repo of the pull request to clone and scan; requires --pr; conflicts with --dir
--pr <N>                  Pull request number; requires --repo
--cache-dir <DIR>         Directory the pull request is cloned under, and left in [default: the temporary directory]
--base <REV>              Revision of the git dir to compare from; requires --dir; conflicts with --full
--full                    Scan every file, not the change from the base or the pull request's merge base
--rules <DIR>             Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>      More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
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

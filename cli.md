# Surface: CLI
Each subcommand, flag, option, and positional argument declaration below
exists exactly as written.

## `jev-review`
```
--db <FILE>               SQLite file holding one review: its dir, base, pull request, metadata, findings, and verdicts
--lock-timeout <SECONDS>  Seconds to wait for another command to release the db [default: 3600]

COMMAND:
  scan     Run jev over a directory, or a clone of a pull request, into a new db: the change from a base, or every file when full; print the scan's summary
  pending  Print each finding without a verdict as a line of JSON, its file relative to the dir
  confirm  Confirm one finding without a verdict
  reject   Reject one finding without a verdict
  show     Print the review once every finding has a verdict; fails while any finding has none
  post     Post the confirmed findings as a review on the pull request and print its URL, once every finding has a verdict; fails while any finding has none
```

## `jev-review scan`
```
--dir <DIR>             Directory to scan in place; findings are relative to it; requires --base or --full; conflicts with --repo
--repo <HOST/ORG/NAME>  GitHub repo of the pull request to clone and scan; requires --pr; conflicts with --dir
--pr <N>                Pull request number; requires --repo
--cache-dir <DIR>       Directory the pull request is cloned under, and left in [default: the temporary directory]
--base <REV>            Revision of the git dir to compare from; requires --dir; conflicts with --full
--full                  Scan every file, not the change from the base or the pull request's merge base
--rules <DIR>           Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>    More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
--metadata <JSON>       JSON value stored with the review and printed by show
```

## `jev-review confirm`
```
INDEX   Index of the finding in the scan's findings, from 1
REASON  One sentence on how the lines match the violation
```

## `jev-review reject`
```
INDEX   Index of the finding in the scan's findings, from 1
REASON  One sentence on why the lines do not break the rule
```

## `jev-review show`
```
--format <json|md>  Print the review as JSON, or as markdown of its confirmed findings [default: json]
```

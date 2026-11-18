# Surface: CLI
Each subcommand, flag, option, and positional argument declaration below
exists exactly as written.

## `jev-code-review`
```
--state <FILE>            SQLite file holding all of one review: its source, clone and head, sandbox, findings, verdicts, and result
--lock-timeout <SECONDS>  Seconds to wait for another command to release the state file [default: 3600]

COMMAND:
  start-review     Prepare a jev review of a pull request or a local git dir in a new state file, run jev over its change, or every file of its head when full, and print its brief and the scan's summary
  confirm-finding  Confirm one jev_scan finding of the review
  reject-finding   Reject one jev_scan finding of the review
  finish-review    Finish the review once every finding has a verdict
  post-review      Post the finished review's confirmed findings as a review on its pull request
  cancel-review    Mark the review cancelled, so no later command changes it
  read-review      Print the finished review's result
  sandbox          Run a bashkit-sandbox command in the review's sandbox
```

## `jev-code-review start-review`
```
--repo <HOST/ORG/NAME>  GitHub repo of the pull request; requires --pr; conflicts with --git-dir
--pr <N>                Pull request number; requires --repo
--cache-dir <DIR>       Directory the review's clone is kept in until the review finishes or is cancelled [default: the temporary directory]
--git-dir <DIR>         Git dir on the host; requires --head; conflicts with --repo
--base <REV>            Revision to compare from; requires --git-dir; conflicts with --full
--head <REV>            Revision to audit; requires --git-dir
--rules <DIR>           Directory of rule sets jev-scan reviews against [default: $JEV_RULES_DIR]; required
--extra-rules <DIRS>    More directories of rule sets, separated by ':' [default: $JEV_RULES_EXTRA]
--full                  Audit every file of the head, not the change from the base
```

## `jev-code-review confirm-finding`
```
INDEX   Index of the finding in jev_scan's file, from 1
REASON  One sentence on how the lines match the violation
```

## `jev-code-review reject-finding`
```
INDEX   Index of the finding in jev_scan's file, from 1
REASON  One sentence on why the lines do not break the rule
```

## `jev-code-review sandbox`
```
COMMAND:
  bash  Run the shell command read from stdin
  read  Read a text file with numbered lines
```

## `jev-code-review sandbox read`
```
--file-path <PATH>        The file to read
--offset <N>              The first line to read, from 1
--limit <N>               The number of lines to read
--max-file-size <BYTES>   The largest file, in bytes, to read a range of lines from [default: 16777216]
```

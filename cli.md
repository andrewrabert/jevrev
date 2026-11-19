# Surface: CLI
Each subcommand, flag, option, and positional argument declaration below
exists exactly as written.

## `jev-code-review`
```
--state <FILE>            SQLite file holding all of one review: its source, clone and head, sandbox, findings, verdicts, and result
--lock-timeout <SECONDS>  Seconds to wait for another command to release the state file [default: 3600]

COMMAND:
  review  Start, judge, and finish the review in the state file
```

## `jev-code-review review`
```
COMMAND:
  start        Prepare a jev review of a pull request or a local git dir in a new state file, run jev over its change, or every file of its head when full, and print its brief and the scan's summary
  jev-confirm  Confirm one jev_scan finding of the review
  jev-reject   Reject one jev_scan finding of the review
  finish       Finish the review once every finding has a verdict, submitting a pull request review's confirmed findings as a review on the GitHub pull request
  cancel       Mark the review cancelled, so no later command changes it
  show         Print the finished review's result
  bash         Run the shell command read from stdin in the review's sandbox
  file-read    Read a text file in the review's sandbox with numbered lines
```

## `jev-code-review review start`
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

## `jev-code-review review jev-confirm`
```
INDEX   Index of the finding in jev_scan's file, from 1
REASON  One sentence on how the lines match the violation
```

## `jev-code-review review jev-reject`
```
INDEX   Index of the finding in jev_scan's file, from 1
REASON  One sentence on why the lines do not break the rule
```

## `jev-code-review review finish`
```
--github-review [<BOOL>]  Submit the confirmed findings as a review on the GitHub pull request [default: true]
```

## `jev-code-review review file-read`
```
--file-path <PATH>        The file to read
--offset <N>              The first line to read, from 1
--limit <N>               The number of lines to read
--max-file-size <BYTES>   The largest file, in bytes, to read a range of lines from [default: 16777216]
```

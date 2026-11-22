# jevrev

jevrev is a CLI that reviews code against rules you write and reports each place the code breaks one. You can use it in two ways:

- **As a linter.** Scan a directory, a git diff, or a GitHub pull request and print the findings.
- **As a review workflow.** Save the findings to a review file, confirm or reject each one with a reason, then print the confirmed findings as markdown or post them as a review on the pull request.

## Setup

```sh
just install
export TYPESAFE_API_KEY=...
export JEVREV_RULES=~/rules/python:~/rules/prose
```

jevrev asks jev whether code breaks the rules in your rule sets. A rule set is a YAML file of rules, each describing a defect with examples of code that has it and code that doesn't. `JEVREV_RULES` lists directories of rule sets, separated by `:`. Each rule set has a `when:` block that names the files it applies to, so a scan of a whole repo checks each file only against the rule sets that match it. [example_rules](example_rules) holds examples.

To scan GitHub or post to it, jevrev needs a GitHub token. See [GitHub](#github).

## Run it as a linter

```sh
jevrev scan src
```

jevrev prints each finding on one line as `file:lines [index] rule probability state`. The state is what of the file jev was given: `file`, the whole file; `hunk`, one hunk of a change; `hunks`, all of a file's changed hunks; or `language`, one range of the file that a language yields, such as a function. The probability is jev's estimate that the lines break the rule. The text of the rule follows on the next line, and a summary line ends the output. Output is in color when stdout is a terminal.

jevrev exits 0 with no findings, 1 with findings, and 2 on any error, so CI or a pre-commit hook can fail on findings. `--exit-zero` makes it exit 0 even with findings.

## Review findings before publishing them

Some of jev's findings are false positives. The review workflow lets you, or an agent, decide on each finding before jevrev posts anything to the pull request.

```sh
# 1. Scan the pull request and create the review file. The file must not already exist.
jevrev -r pr12.redb scan-github ORG/NAME --pr 12

# 2. List the findings that still need a verdict.
jevrev -r pr12.redb pending

# 3. Confirm or reject each finding by its [index], with a one-sentence reason.
jevrev -r pr12.redb confirm 1 "The handler returns an error response instead of raising"
jevrev -r pr12.redb reject 2 "The error return is on an unchanged line"

# 4. Publish. Both commands refuse to run until every finding has a verdict.
jevrev -r pr12.redb show --format md   # print the confirmed findings as markdown
jevrev -r pr12.redb submit-github      # post them as a pull request review with inline comments
```

Each step in more detail:

1. **Scan with `-r`/`--review FILE`.** The scan creates FILE and saves its findings there. FILE must not exist, and jevrev checks this before it sends any request. If the scan fails, jevrev leaves no FILE behind. To scan again, delete FILE or choose another name.
2. **List the undecided findings with `pending`.**
3. **Give each finding a verdict with `confirm INDEX REASON` or `reject INDEX REASON`.** INDEX is the number in brackets in the scan's output. You can't change a verdict once you give it. A confirmed finding's reason becomes the text of its comment on the pull request, so write it for the pull request's author.
4. **Publish with `show` or `submit-github`.** `show` prints the review as JSON, or with `--format md` as markdown of the confirmed findings grouped by rule. `submit-github` posts the confirmed findings as a review on the pull request and prints the review's URL. Both fail while any finding has no verdict.

`-r` is a global flag, so it goes before the command. Every command other than a scan requires it. A command locks FILE while it runs, and a second command on the same FILE fails at once instead of waiting.

The review file records the source and the commit ids the scan resolved. `submit-github` comments on the scanned commit even if someone has pushed to the pull request since.

`submit-github` works only on a review that `scan-github --pr` created. It requests changes on someone else's pull request. On your own, it only comments, because GitHub refuses a request for changes from a pull request's author. Findings on changed lines become inline comments. Findings in files the pull request didn't change go in the review's body. jevrev posts nothing when no finding was confirmed.

## Choose what to scan

| To review | Run |
|---|---|
| A file, or a directory's files, on disk | `scan PATH` |
| A git directory's files at a revision | `scan-git DIR --rev REV` |
| The changes from REV to the files on disk, as `git diff REV` shows them | `scan-git DIR --diff REV` |
| The changes from A to B | `scan-git DIR --diff A..B` |
| The changes from merge-base(A, B) to B, as a pull request from B into A shows them | `scan-git DIR --diff A...B` |
| A GitHub repo at the head of its default branch | `scan-github ORG/NAME` |
| A GitHub repo at a revision | `scan-github ORG/NAME --rev REV` |
| A GitHub repo's changes from A, or from merge-base(A, B), to B | `scan-github ORG/NAME --diff A..B` or `A...B` |
| A GitHub pull request's changes | `scan-github ORG/NAME --pr N` |

A rule set's `when` selects the files it reviews. Each selected file is reviewed whole. A diff scan also reviews each changed hunk, and all of a changed file's hunks together. Each of the file's languages also yields its ranges, such as each function: every range, or in a diff scan only the ranges the diff touches. A file that a language cannot parse gets no ranges in it. Every rule set that selects a file is asked about all of its states; `when` only selects files.

No scan writes to the code it reads. `scan-git` reads files from the git repository without checking anything out, and `scan-github` reads them through the GitHub API without cloning.

`scan-git --diff REV` takes the files on disk as git does: every tracked file, and the untracked files that the repository's `.gitignore` files, `.git/info/exclude` and the `core.excludesFile` of your git configuration leave in. Every other `scan-git` scan, and every `scan-github` scan, reads committed files only, so no ignore rules apply to it.

## Reference

### Scan flags

| Flag | Environment variable | Purpose |
|---|---|---|
| `--rules DIR` | `JEVREV_RULES` | Directory of rule sets. Repeat for more. Any `--rules` replaces `JEVREV_RULES`. |
| `-i`/`--include PATTERN` | | Scan only files PATTERN matches, in gitignore syntax. Repeat for more. |
| `-e`/`--exclude PATTERN` | | Leave out files PATTERN matches. Repeat for more. Wins over `--include`. |
| `--no-ignore` | | `scan` only. Also scan files that `.ignore` files match, and inside a git repository files that its `.gitignore` files, `.git/info/exclude` or the global excludes file match. `scan` given a single file scans it regardless. |
| `--exit-zero` | | Exit 0 even with findings. |
| `-q`/`--quiet` | | Print the findings and nothing else. |
| `--api-key` | `TYPESAFE_API_KEY` | TypeSafe API key. Required. |
| `--base-url` | `TYPESAFE_BASE_URL` | TypeSafe API URL. |
| `--model` | `TYPESAFE_DEFAULT_MODEL` | Jev model to ask. |
| `--timeout` | `TYPESAFE_TIMEOUT` | Seconds each connection and read may take, 90 by default. `inf` means no limit. |
| `-c`/`--concurrency N` | `JEVREV_CONCURRENCY` | Most jev requests in flight at once, 8 by default. |

`--env-file` or `JEVREV_ENV_FILE` names a dotenv file of the variables above. A flag wins over the environment, and a variable set in the environment wins over the file.

### GitHub

jevrev reaches GitHub on the host `--github-host` or `GITHUB_HOST` names, or `github.com` when neither is set, with the token in `--github-token` or `GITHUB_TOKEN`. A review file records its host, and `submit-github` posts there.

## Development

Development tasks run through [just](https://github.com/casey/just). `just` with no recipe lists them.

| Recipe | Purpose |
|---|---|
| `just ci` | Run the tests, clippy with warnings denied, the `cargo fmt` check, and check that `Cargo.lock` is up to date. Run it before you commit. |
| `just test` | Run the tests. |
| `just check` | Run `cargo check` on all targets. |
| `just clippy` | Run clippy with warnings denied. |
| `just fmt` | Format the code. |
| `just build` | Build jevrev. |
| `just run` | Run jevrev. |
| `just install` | Install jevrev from this checkout with `cargo install`. |
| `just outdated` | Fail if a direct dependency is outdated. Requires `cargo-outdated`. |

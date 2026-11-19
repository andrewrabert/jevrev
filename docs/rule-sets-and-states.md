# jevrev Rule Sets and Scan States

**Version 1.0**

jevrev reviews the text files of a source against rule sets: files of rules, each describing a
defect. A scan turns each file into states, and asks the judge whether each state breaks each rule
of each rule set that reviews it.

This document specifies the rule set file format, how a scan selects the files a rule set reviews,
which states a scan builds from each selected file, and which rule sets are asked about each state.

The keywords MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be read as described in RFC 2119.

## 1. Scope

### Goals

1. A reader can write a valid rule set file, and predict which files it reviews.
2. A reader can predict, for any file of a scan, every state the scan builds from it and every rule
   set asked about each state.
3. A reader can implement file selection and state building from this document alone.

### Non-goals

- How a scan obtains its source: a directory, a git revision, a git diff, or a hosted repository.
- How ignore files, `--include` and `--exclude` decide the scope, beyond §5.1's use of it.
- The command line, settings and environment variables.
- How the judge decides whether a state breaks a rule, and how it scores the answer.
- The printed output, the review file, and the review workflow.

## 2. Terminology

- **Source**: the files a scan reads, at one version, each named by a source path.
- **Source path**: a file's path from the top of the source, `/`-separated, never empty.
- **Scope**: the source paths a scan may review, after ignore files, `--include` and `--exclude`.
- **Rule set**: one rule set file (§3).
- **Rule**: one entry of a rule set's `rules` (§3.3).
- **Selector**: one entry of a rule set's `when` (§4).
- **Language**: one of the languages of §6, which a selector can name.
- **Detected language**: a language a file is in by its name, suffix or first line (§6.1).
- **Range**: a run of consecutive lines of a file, numbered from 1, that a language yields (§6.2).
- **Change**: a diff of one file: its hunks, between the file before the change and the file after
  it.
- **Hunk**: one hunk of a change, with its added, removed and context lines.
- **Untracked file**: a file of the source that is new to the version a diff starts from.
- **Scan target**: a file a scan considers for review (§5.1).
- **State**: what one judge request is about: some or all of one file (§7).
- **Finding**: one rule a state breaks, as a scan reports it (§9).

## 3. Rule set files

### 3.1 Location

A scan reads its rule sets from one or more rules directories.

- Every file whose name ends in `.yaml`, in a rules directory or any directory below it, MUST be read
  as a rule set.
- A file ending in `.yml` or any other suffix MUST NOT be read as a rule set.
- A file reached through two rules directories MUST count as one rule set.
- A rules directory that does not exist, or is not a directory, MUST fail the scan.

### 3.2 Format

A rule set file MUST be one YAML document, a mapping, encoded as UTF-8. A file that cannot be read
or parsed, or that breaks this section, MUST fail the scan.

| Key | Type | Absent ⇒ |
|---|---|---|
| `description` | string | No description. It has no effect on a scan. |
| `when` | list of selectors (§4) | The rule set reviews no file. |
| `error_threshold` | number from 0 to 1, both included | Required. A file without it fails the scan. |
| `rules` | list of rules (§3.3) | Required. A file without it fails the scan. |

A key not in this table MUST fail the scan.

### 3.3 Rules

| Key | Type | Absent ⇒ |
|---|---|---|
| `flags` | string: the defect the rule flags | Required. |
| `good` | list of strings: examples of code that keeps the rule | No examples. |
| `bad` | list of strings: examples of code that breaks the rule | No examples. |

A key not in this table MUST fail the scan.

A rule's id is the rule set file's name, `#`, and the rule's position in `rules`, from 1, such as
`text.yaml#1`. The id names the file, not its directory.

## 4. Selectors

A rule set reviews a file when at least one of its selectors matches the file. An empty `when` list
matches no file.

| Key | Type | Absent or empty ⇒ |
|---|---|---|
| `language` | list of language names (§6) | Any file, in any language or none. |
| `matches` | list of gitignore patterns | Any source path. |
| `exists` | list of gitignore patterns | No requirement. |

A key not in this table MUST fail the scan. A name in `language` that §6 does not list MUST fail the
scan.

A selector matches a file when all of the following hold:

1. **`language`**: at least one of the file's detected languages (§6.1) is in the list.
2. **`matches`**: the patterns, read together as one gitignore file relative to the top of the
   source, match the file's source path. A later `!` pattern MAY re-include a path an earlier
   pattern left out.
3. **`exists`**: each pattern matches at least one source path of the source after ignore files,
   whether or not that path is in scope.

`language` matches on detected languages only. A file matches a `language` selector whether or not
the file is valid in that language; validity decides only its ranges (§7.4).

Selectors decide which files a rule set reviews, and nothing else. They MUST NOT affect which states
a reviewed file has, or which of them a rule set is asked about.

## 5. Scans

### 5.1 Scan targets

| Scan | Scan targets |
|---|---|
| No diff | Every file of the source. |
| Diff | Every file the diff adds or modifies, and every untracked file of the source. |

A file a diff deletes MUST NOT be a scan target. A file the diff marks binary, or whose path, added
lines or context lines are not UTF-8, MUST NOT be a scan target.

A scan target MUST be reviewed if and only if both of the following hold:

1. Its source path is in scope.
2. At least one rule set reviews it (§4).

The same two conditions MUST decide selection in every kind of scan. No other property of a file,
including its detected languages, MAY affect whether it is reviewed.

### 5.2 File text

A reviewed file MUST be UTF-8 text. A reviewed file that is not MUST fail the scan, naming the file.
It MUST NOT be skipped.

### 5.3 Untracked files

In a diff scan, an untracked file MUST be treated as a change that adds the whole file: one hunk
whose every line is added, or no hunk for an empty file.

## 6. Languages

### 6.1 Detection

A file's detected languages are every language of this table that its file name, its suffix, or
its first line marks. A file MAY have none, one or several.

| Name | File names | Suffixes | First lines | Test marker |
|---|---|---|---|---|
| `rust` | — | `.rs` | — | `#[cfg(test)]` |
| `python` | — | `.py` | `#!…/python…`, `#!…/env … python…`, `#!/usr/bin/env … uv … run` | — |
| `sh` | — | — | `#!…/sh`, `#!…/env … sh` | — |
| `just` | `justfile`, `Justfile`, `.justfile` | `.just` | — | — |
| `markdown` | — | `.md` | — | — |

The test marker is an exact line. The first line of a file that equals it, and every line after,
are test code (§7.4).

### 6.2 Ranges

A language yields the ranges of a whole file. For a file valid in the language:

1. The ranges MUST cover every line of the file.
2. Two ranges MUST NOT share a line.
3. The ranges MUST be ordered by line.

A file not valid in the language has no ranges in it.

| Language | Valid when | Ranges |
|---|---|---|
| `rust` | The file has no syntax error. | Each top-level item; each item of an inline module; each method of an impl or trait that has methods, in place of the impl or trait. |
| `python` | The file has no syntax error. | Each top-level statement; for a class with methods, its head up to the first method, then each method. |
| `sh` | The file has no syntax error. | Each function; each run of top-level commands between functions. |
| `just` | Always. | The whole file, as one range. |
| `markdown` | Always. | The whole file, as one range. |

For `rust`, `python` and `sh`:

- The comments directly above an item, statement or function, and for `rust` its attributes, with
  no blank line between, MUST belong to its range.
- A run of `rust` consts and statics, or of `python` assignments, with at most one blank line and
  nothing else between each and the next, MUST be one range.
- Lines that belong to no item, statement or function, such as blank lines, imports, and comments
  between them, MUST form ranges of their own, one per run of such lines.
- The `sh` first line, when it begins with `#!`, MUST NOT belong to a function's or a run's range.

## 7. States

A scan builds three kinds of states from each reviewed file. The kinds are independent: no kind is
built from another. Every rule set that reviews the file is asked about all of them (§8).

| Kind | Name in findings | Holds |
|---|---|---|
| File state | `file` | The whole file. |
| Hunk state | `hunk` | One hunk of the file's change. |
| Hunks state | `hunks` | Every hunk of the file's change. |
| Language state | `language` | One range a language yields. |

### 7.1 File states

Every reviewed file MUST have exactly one file state. It holds every line of the file. Its lines are
1 to the last line, or line 1 for an empty file.

### 7.2 Hunk states

A reviewed file with no change MUST have no hunk state.

A reviewed file with a change MUST have one hunk state per hunk. A hunk state MUST hold every line of
its hunk: added, removed and context. A hunk MUST NOT be left out for having no added line.

A hunk state's lines are the hunk's lines in the file after the change. A hunk that leaves no line
in the file after the change is at the line it follows, or line 1 at the top of the file.

Each hunk SHOULD carry 20 unchanged lines of context on each side of its changed lines, fewer at the
edges of the file. Hunks whose context would overlap MUST be one hunk.

### 7.3 Hunks states

A reviewed file whose change has at least one hunk MUST have exactly one hunks state, holding every
hunk of the change. Its lines run from its first hunk state's first line to its last hunk state's
last line. Any other file MUST have none.

### 7.4 Language states

A reviewed file MUST have language states of each of its detected languages that it is valid in
(§6.2), and of no other language.

A file not valid in a language MUST have no language state of it. Its file, hunk and hunks states
MUST NOT depend on that.

The **candidate ranges** of a language are:

| Scan | Candidate ranges |
|---|---|
| No diff | Every range of the file. |
| Diff | The ranges of the file after the change that the change touches (§7.5). |

A language state MUST be built from each candidate range, except that:

- A range that starts at or after the first test code line (§6.1) MUST NOT become a state.
- A range that spans the first test code line MUST end at the line before it.
- A range whose lines are all blank MUST NOT become a state, because it holds nothing to judge.

A language state holds the lines of its range in the file after any change.

### 7.5 Ranges a change touches

This section applies to one language and one changed file. The **file before the change** is the
file the change's removed and context lines, and the unchanged lines outside its hunks, describe.

1. If the file after the change is not valid in the language, no range is touched.
2. If the file before the change is not valid in the language, every range of the file after the
   change MUST be touched. A change that makes a file valid is treated as all new code.
3. Otherwise, a range of the file after the change MUST be touched when it holds at least one of the
   following lines:
   - a line the change adds;
   - a line the change keeps from a range of the file before the change that lost at least one line.

A range of the file before the change that the change removes entirely MUST NOT touch any range,
because nothing of it remains to judge.

### 7.6 Presentation (informative)

This subsection is informative, not normative.

The judge receives each state as a unified diff of its file. File and language states show their
lines as added, under a hunk header that gives their position in the file. A hunk state shows its
hunk's lines without the hunk header. A hunks state shows every hunk with its header.

## 8. Which rule sets are asked about each state

Every rule set that reviews a file MUST be asked about every state of the file, of every kind
(§7). How a rule set's selectors matched the file MUST NOT change which states it is asked about.

Each rule set MUST be asked about a state once, for all its rules together.

## 9. Findings

A rule a state breaks is a finding when the judge's probability that the state breaks it is at least
the rule set's `error_threshold`.

A finding names:

| Field | Value |
|---|---|
| `file` | The state's source path. |
| `start`, `end` | The state's first and last lines. |
| `rule` | The rule's id (§3.3). |
| `probability` | The judge's probability, rounded to 2 places. |
| `flags`, `violation`, `accepted` | The rule's `flags`, `bad` joined by spaces, and `good` joined by spaces. |
| `state` | The state's kind, by its name in findings (§7). |

A scan MUST NOT report the same finding twice in either of these cases:

1. Two rule sets asked about one state have a rule with the same `flags`: only the first rule set's
   finding is reported.
2. A file or hunks state's finding has the same `flags` as a hunk or language state's finding of the
   same file: only the hunk or language state's finding is reported, because it names the lines.

## 10. Guarantees

A caller MAY rely on all of the following.

1. **One selection.** Whether a file is reviewed depends only on scope and on the rule sets'
   selectors, the same way in every kind of scan.
2. **No silent skip.** A file a rule set selects is reviewed, or the scan fails naming it. It is
   never dropped for having no detected language or for not being text.
3. **Every reviewed file is asked about whole.** Every rule set that reviews a file is asked about
   its file state.
4. **Selection only selects.** Which states a reviewed file has, and which rule sets are asked
   about them, depend only on the file, its change and its detected languages, never on which
   selector matched it.
5. **A broken file still gets reviewed.** A file not valid in a language loses only that language's
   states.
6. **Removals count.** A range that lost lines to a change is reviewed as it is after the change,
   even when the change added nothing to it.

## 11. Conformance

An implementation conforms to this document if:

1. It reads and refuses rule set files as §3 states.
2. It matches selectors as §4 states.
3. It selects files as §5.1 states, and fails on a non-UTF-8 reviewed file as §5.2 states.
4. It detects languages and yields ranges as §6 states.
5. It builds exactly the states §7 states for each reviewed file.
6. It sends each state to exactly the rule sets §8 states.
7. It reports findings as §9 states.

An implementation MAY:

- Build and judge states in any order. A caller MUST NOT depend on the order.
- Refuse to judge a state too large for the judge, reporting that request as failed.
- Add languages. A new language MUST follow §6.

An implementation MUST NOT:

- Drop a selected file without failing the scan.
- Ask a rule set about only some of the states of a file it reviews.
- Build a language state from a file not valid in its language.

## 12. Deferred

The following are intentionally left to a later revision:

- **Rule id collisions.** Two rule sets with the same file name in different directories give their
  rules the same ids. This document does not say how to tell them apart.
- **Test code beyond the marker.** Only the first test marker line limits language states. Test
  code a language could recognize elsewhere, such as a test module that does not start at a marker
  line, is not left out.
- **The size limit of a state.** §11 permits refusing a large state, but this document sets no size.
- **Context lines.** §7.2 recommends 20 lines of context. Whether every kind of diff scan keeps that
  number is not promised.
- **A finding's language.** A language state's finding names its kind, not which language yielded
  it.
- **Order of findings.** This document does not specify an order.

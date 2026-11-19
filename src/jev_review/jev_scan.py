"""Scan code and text with jev rule sets: every top-level item of the files in a known language
under a root, or the hunks of any text file changed since a revision. Each file is reviewed
against the rule sets under the rules directory whose `when` matches it."""

import asyncio
import dataclasses
import enum
import functools
import json
import os
import pathlib
import re
import sys

import pathspec
import pydantic
import tree_sitter
import tree_sitter_bash
import tree_sitter_python
import tree_sitter_rust
import unidiff
import yaml

import jev_review.jev_ask
import jev_review.rules

# Siblings that belong to the item after them.
LEADING = {"line_comment", "block_comment", "attribute_item"}
PYTHON_LEADING = {"comment"}

# Items with nothing for the rule set to judge.
SKIPPED = {
    "use_declaration",
    "extern_crate_declaration",
    "inner_attribute_item",
    "line_comment",
    "block_comment",
    "attribute_item",
    "empty_statement",
}

PYTHON_SKIPPED = {
    "import_statement",
    "import_from_statement",
    "future_import_statement",
    "comment",
    "pass_statement",
}

# Top-level sh commands that change how every other part of the script runs; each item of a
# script carries the ones outside it as context.
SHELL_SETUP = {"set", "trap"}

RUST = tree_sitter.Language(tree_sitter_rust.language())
PYTHON = tree_sitter.Language(tree_sitter_python.language())
BASH = tree_sitter.Language(tree_sitter_bash.language())


@dataclasses.dataclass(frozen=True)
class Syntax:
    # Splits a whole file into items; with none, the whole file is one item.
    parser: tree_sitter.Language | None = None
    # A line marking where test code starts; it and everything after it is left out.
    test_marker: str | None = None


LanguageName = jev_review.rules.Language

# The languages a rule set's `when.language` can name.
SYNTAXES = {
    LanguageName.RUST: Syntax(parser=RUST, test_marker="#[cfg(test)]"),
    LanguageName.PYTHON: Syntax(parser=PYTHON),
    LanguageName.SH: Syntax(parser=BASH),
    LanguageName.JUST: Syntax(),
    LanguageName.MARKDOWN: Syntax(),
}

PLAIN = Syntax()

SUFFIXES = {
    ".rs": LanguageName.RUST,
    ".py": LanguageName.PYTHON,
    ".just": LanguageName.JUST,
    ".md": LanguageName.MARKDOWN,
}

JUSTFILE_NAMES = ("justfile", "Justfile", ".justfile")

# A first line that matches one of these marks a Python script, or failing that a POSIX sh
# script.
PYTHON_SHEBANGS = (
    re.compile(r"^#!\S*/python[\d.]*(\s|$)"),
    re.compile(r"^#!\S*/env\s+(-\S+\s+)*python[\d.]*(\s|$)"),
    re.compile(r"^#!/usr/bin/env.*uv.*run"),
)
SHELL_SHEBANGS = (
    re.compile(r"^#!\S*/sh(\s|$)"),
    re.compile(r"^#!\S*/env\s+(-\S+\s+)*sh(\s|$)"),
)


def has_shebang(source, patterns):
    first = source.split(b"\n", 1)[0].decode(errors="replace")
    return any(pattern.search(first) for pattern in patterns)


def detect_language(name, source) -> LanguageName | None:
    """The language of a file, a key of SYNTAXES, or None."""
    path = pathlib.PurePath(name)
    if path.name in JUSTFILE_NAMES:
        return LanguageName.JUST
    if path.suffix in SUFFIXES:
        return SUFFIXES[path.suffix]
    if has_shebang(source, PYTHON_SHEBANGS):
        return LanguageName.PYTHON
    if has_shebang(source, SHELL_SHEBANGS):
        return LanguageName.SH
    return None


@functools.cache
def read_set(path):
    try:
        return jev_review.rules.RuleSet.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (OSError, yaml.YAMLError, pydantic.ValidationError) as e:
        raise UserError(f"rule set {path}: {e}") from e


@dataclasses.dataclass(frozen=True)
class Condition:
    """One `when` entry; an empty field matches every file."""

    languages: frozenset[LanguageName]
    patterns: pathspec.GitIgnoreSpec | None
    # Each matches at least one path under the root.
    exists: tuple[pathspec.Pattern, ...]

    def holds_under(self, root):
        return all(
            next(
                pathspec.GitIgnoreSpec([pattern]).match_tree_files(root), None
            )
            is not None
            for pattern in self.exists
        )

    def matches(self, detected, name):
        return (not self.languages or detected in self.languages) and (
            self.patterns is None or self.patterns.match_file(name)
        )


@dataclasses.dataclass(frozen=True)
class RuleSet:
    path: pathlib.Path
    # The set reviews a file when any of these matches it.
    conditions: tuple[Condition, ...]

    def under(self, root):
        """The set as it applies under `root`: only the conditions whose paths exist there."""
        return dataclasses.replace(
            self,
            conditions=tuple(
                c for c in self.conditions if c.holds_under(root)
            ),
        )


@functools.cache
def rule_sets(rules):
    """Every rule set under `rules` with a `when`, sorted by path."""
    if not rules.is_dir():
        raise UserError(f"rule sets directory {rules} is not a directory")
    found = []
    for path in sorted(rules.rglob("*.yaml")):
        parsed = read_set(path)
        if parsed.when is None:
            continue
        found.append(
            RuleSet(
                path=path,
                conditions=tuple(
                    Condition(
                        languages=frozenset(when.language),
                        patterns=pathspec.GitIgnoreSpec(when.matches)
                        if when.matches
                        else None,
                        exists=when.exists,
                    )
                    for when in parsed.when
                ),
            )
        )
    return tuple(found)


def env_path(name):
    """The path in the environment variable name, or None when it is unset or empty."""
    value = os.environ.get(name)
    return pathlib.Path(value) if value else None


def path_list(value):
    """The paths of a list separated by os.pathsep, as in PATH, without its empty entries."""
    return [pathlib.Path(entry) for entry in value.split(os.pathsep) if entry]


def sets_under(sets, root):
    """The rule sets that can apply under `root`, each with only its conditions that do."""
    return tuple(s for s in (s.under(root) for s in sets) if s.conditions)


@dataclasses.dataclass(frozen=True)
class Language:
    # The rule sets every item of the file is reviewed against.
    sets: tuple[pathlib.Path, ...]
    # Splits a whole file into items; with none, the whole file is one item.
    parser: tree_sitter.Language | None = None
    # A line marking where test code starts; it and everything after it is left out.
    test_marker: str | None = None


def language_of(name, source, sets):
    """How a file is reviewed against `sets`; `name` is its path from the root."""
    detected = detect_language(name, source)
    syntax = SYNTAXES.get(detected, PLAIN)
    return Language(
        sets=tuple(
            rule_set.path
            for rule_set in sets
            if any(c.matches(detected, name) for c in rule_set.conditions)
        ),
        parser=syntax.parser,
        test_marker=syntax.test_marker,
    )


def in_whole_scan(name, source):
    """Whether a whole-directory scan reviews the file: it has a language. With --base, every
    changed text file is reviewed."""
    return detect_language(name, source) is not None


class UserError(Exception):
    pass


class Evaluation(enum.StrEnum):
    """What one item of a change evaluates: the full diff, each file's diff, or each hunk of it."""

    DIFF = "diff"
    FILE = "file"
    HUNK = "hunk"


class ItemKind(enum.StrEnum):
    """What an item is; MERGED kinds next to each other are reviewed as one item."""

    CONST = "const"
    STATIC = "static"
    ASSIGNMENT = "assignment"
    DEFINITION = "definition"
    TOP_LEVEL = "top-level"
    HUNK = "hunk"
    FILE = "file"
    DIFF = "diff"


MERGED = frozenset({ItemKind.CONST, ItemKind.STATIC, ItemKind.ASSIGNMENT})

# The kind of each tree-sitter node type that is not a definition.
NODE_KINDS = {
    "const_item": ItemKind.CONST,
    "static_item": ItemKind.STATIC,
    "assignment": ItemKind.ASSIGNMENT,
}


def inside(root, path):
    """Whether path, its symlinks followed, is under root, which is resolved."""
    return path.resolve().is_relative_to(root)


Answers = pydantic.RootModel[dict[str, float]]


class DiffHunk(pydantic.BaseModel):
    header: str
    # The hunk's added and context lines, each with its `+` or ` ` prefix; removed lines are left
    # out.
    lines: list[str]


class Item(pydantic.BaseModel):
    file: str
    start: int
    end: int
    name: str
    kind: ItemKind = ItemKind.DEFINITION
    context: str
    text: str
    # Changed hunks as git printed them; reviewed in place of `text` when there are any.
    hunks: list[DiffHunk] = []
    # Whether the diff leaves out its `@@` lines.
    headless: bool = False
    # The whole state jev is asked about, in place of the diff.
    state: str | None = None
    # The rule sets the item is reviewed against.
    sets: tuple[pathlib.Path, ...] = ()
    # How much of the diff the item evaluates: a hunk, a file's hunks, or the full diff.
    evaluated: Evaluation | None = None

    @property
    def diff(self):
        hunks = self.hunks or [
            DiffHunk(
                header=f"@@ -0,0 +{self.start},{len(self.text.splitlines())} @@ "
                f"{self.context}".rstrip(),
                lines=[f"+{line}" for line in self.text.splitlines()],
            )
        ]
        return "\n".join(
            [
                f"diff --git a/{self.file} b/{self.file}",
                f"--- a/{self.file}",
                f"+++ b/{self.file}",
                *(
                    line
                    for hunk in hunks
                    for line in (
                        hunk.lines
                        if self.headless
                        else [hunk.header, *hunk.lines]
                    )
                ),
            ]
        )


class Finding(pydantic.BaseModel):
    file: str
    start: int
    end: int
    item: str
    # The rule set's file name and the rule's position in it, such as text.yaml#1.
    rule: str
    probability: float
    flags: str
    violation: str
    accepted: str
    evaluated: Evaluation | None = None


class AskFailed(Exception):
    def __init__(self, item, stderr):
        super().__init__(item, stderr)
        self.item = item
        self.stderr = stderr

    def __str__(self):
        return f"{self.item.file}:{self.item.start} {self.item.name}: {self.stderr.strip()}"


def json_dumps(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


async def run(*argv, stdin=None, cwd=None):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin)
    return proc.returncode, stdout.decode(), stderr.decode()


class git:
    @staticmethod
    async def current_repository():
        code, stdout, _ = await run(
            "git", "rev-parse", "--show-toplevel", cwd=pathlib.Path.cwd()
        )
        if code != 0:
            raise UserError(
                "the current directory is outside a git repository; give --root"
            )
        return pathlib.Path(stdout.strip())

    @staticmethod
    async def in_repository(root):
        code, stdout, _ = await run(
            "git", "rev-parse", "--is-inside-work-tree", cwd=root
        )
        return code == 0 and stdout.strip() == "true"

    @staticmethod
    async def merge_base(root, base):
        """The merge-base of `base` and HEAD."""
        code, stdout, stderr = await run(
            "git", "merge-base", base, "HEAD", cwd=root
        )
        if code != 0:
            raise UserError(f"git merge-base {base} HEAD: {stderr.strip()}")
        return stdout.strip()

    @staticmethod
    async def diff(root, revision, context, pathspecs):
        """The diff from `revision` to the working tree of the files matching `pathspecs`, with
        `context` as git diff options for its context lines."""
        code, stdout, stderr = await run(
            "git",
            "diff",
            *context,
            "--no-color",
            "--no-ext-diff",
            "--relative",
            revision,
            "--",
            *pathspecs,
            cwd=root,
        )
        if code != 0:
            raise UserError(f"git diff {revision}: {stderr.strip()}")
        return stdout

    @staticmethod
    async def untracked_files(root, pathspecs):
        """The untracked files matching `pathspecs`, leaving out those git ignores."""
        code, stdout, stderr = await run(
            "git",
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            "--",
            *pathspecs,
            cwd=root,
        )
        if code != 0:
            raise UserError(
                f"git ls-files --others in {root}: {stderr.strip()}"
            )
        return [name for name in stdout.split("\0") if name]

    @staticmethod
    async def tracked_files(root, pathspecs):
        """The tracked and untracked files matching `pathspecs`, leaving out those git ignores."""
        code, stdout, stderr = await run(
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *pathspecs,
            cwd=root,
        )
        if code != 0:
            raise UserError(f"git ls-files in {root}: {stderr.strip()}")
        return [name for name in stdout.split("\0") if name]


def is_test(node, source):
    return node.type == "attribute_item" and b"cfg(test)" in source[
        node.start_byte : node.end_byte
    ].replace(b" ", b"")


def leading(node, kinds=LEADING):
    """The comments and attributes directly above `node`, top first."""
    found = []
    sibling = node.prev_sibling
    while sibling is not None and sibling.type in kinds:
        below = found[0] if found else node
        if below.start_point[0] - sibling.end_point[0] > 1:
            break
        found.insert(0, sibling)
        sibling = sibling.prev_sibling
    return found


def name_of(node, source):
    name = node.child_by_field_name("name")
    if name is None:
        name = node.child_by_field_name("type")
    label = source[name.start_byte : name.end_byte].decode() if name else ""
    return f"{node.type.removesuffix('_item')} {label}".strip()


def header_of(node, source):
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return " ".join(source[node.start_byte : end].decode().split())


def items(nodes, source, path, context=""):
    lines = source.decode().splitlines()
    for node in nodes:
        if node.type in SKIPPED:
            continue
        above = leading(node)
        if any(is_test(a, source) for a in above):
            continue
        body = node.child_by_field_name("body")
        if node.type == "mod_item" and body is not None:
            yield from items(
                nodes=body.named_children,
                source=source,
                path=path,
                context=header_of(node, source),
            )
            continue
        if node.type in ("impl_item", "trait_item") and body is not None:
            methods = [
                c
                for c in body.named_children
                if c.type in ("function_item", "function_signature_item")
            ]
            if methods:
                yield from items(
                    nodes=methods,
                    source=source,
                    path=path,
                    context=header_of(node, source),
                )
                continue
        start = (above[0] if above else node).start_point[0]
        end = node.end_point[0]
        yield Item(
            file=path,
            start=start + 1,
            end=end + 1,
            name=name_of(node, source),
            kind=NODE_KINDS.get(node.type, ItemKind.DEFINITION),
            context=context,
            text="\n".join(lines[start : end + 1]),
        )


def python_name_of(node, source):
    if node.type == "expression_statement" and node.named_children:
        statement = node.named_children[0]
        target = statement.child_by_field_name("left")
        if statement.type == "assignment" and target is not None:
            return f"assignment {source[target.start_byte : target.end_byte].decode()}"
        return statement.type
    name = node.child_by_field_name("name")
    label = source[name.start_byte : name.end_byte].decode() if name else ""
    return f"{node.type.removesuffix('_definition')} {label}".strip()


def python_kind_of(node):
    if node.type == "expression_statement" and node.named_children:
        return NODE_KINDS.get(node.named_children[0].type, ItemKind.DEFINITION)
    return ItemKind.DEFINITION


def python_items(nodes, source, path, context=""):
    """Each top-level statement of a Python file, and each method of a class apart from its head."""
    lines = source.decode().splitlines()

    def item(first, last, name, kind=ItemKind.DEFINITION):
        return Item(
            file=path,
            start=first + 1,
            end=last + 1,
            name=name,
            kind=kind,
            context=context,
            text="\n".join(lines[first : last + 1]),
        )

    for node in nodes:
        if node.type in PYTHON_SKIPPED:
            continue
        above = leading(node, PYTHON_LEADING)
        start = (above[0] if above else node).start_point[0]
        definition = node.child_by_field_name("definition") or node
        name = python_name_of(definition, source)
        body = definition.child_by_field_name("body")
        if definition.type == "class_definition" and body is not None:
            methods = [
                c
                for c in body.named_children
                if (c.child_by_field_name("definition") or c).type
                == "function_definition"
            ]
            if methods:
                first = leading(methods[0], PYTHON_LEADING) or [methods[0]]
                yield item(
                    first=start, last=first[0].start_point[0] - 1, name=name
                )
                yield from python_items(
                    nodes=methods,
                    source=source,
                    path=path,
                    context=header_of(definition, source),
                )
                continue
        yield item(
            first=start,
            last=node.end_point[0],
            name=name,
            kind=python_kind_of(definition),
        )


def shell_items(nodes, source, path):
    """Each function of a sh script, and each run of top-level code between them. Every item
    carries the script's top-level set and trap commands that lie outside it as context."""
    lines = source.decode().splitlines()
    # The shebang is not a comment for the rule sets to judge.
    first_line = 1 if lines and lines[0].startswith("#!") else 0
    setup = []
    for node in nodes:
        name = (
            node.child_by_field_name("name")
            if node.type == "command"
            else None
        )
        if (
            name is not None
            and source[name.start_byte : name.end_byte].decode() in SHELL_SETUP
        ):
            setup.append(node)

    def item(first, last, name, kind=ItemKind.DEFINITION):
        start = max(
            (leading(first, PYTHON_LEADING) or [first])[0].start_point[0],
            first_line,
        )
        end = last.end_point[0]
        return Item(
            file=path,
            start=start + 1,
            end=end + 1,
            name=name,
            kind=kind,
            context="; ".join(
                " ".join(source[n.start_byte : n.end_byte].decode().split())
                for n in setup
                if not start <= n.start_point[0] <= end
            ),
            text="\n".join(lines[start : end + 1]),
        )

    run = []
    for node in nodes:
        if node.type == "comment":
            continue
        if node.type != "function_definition":
            run.append(node)
            continue
        if run:
            yield item(
                first=run[0],
                last=run[-1],
                name=ItemKind.TOP_LEVEL,
                kind=ItemKind.TOP_LEVEL,
            )
            run = []
        name = node.child_by_field_name("name")
        label = (
            source[name.start_byte : name.end_byte].decode() if name else ""
        )
        yield item(first=node, last=node, name=f"function {label}".strip())
    if run:
        yield item(
            first=run[0],
            last=run[-1],
            name=ItemKind.TOP_LEVEL,
            kind=ItemKind.TOP_LEVEL,
        )


def file_items(name, source, language):
    return [
        item.model_copy(update={"sets": language.sets})
        for item in parsed_items(name=name, source=source, language=language)
    ]


def parsed_items(name, source, language):
    match language.parser:
        case None:
            lines = source.decode().splitlines()
            return [
                Item(
                    file=name,
                    start=1,
                    end=max(len(lines), 1),
                    name=ItemKind.FILE,
                    kind=ItemKind.FILE,
                    context="",
                    text="\n".join(lines),
                )
            ]
        case parser if parser is PYTHON:
            tree = tree_sitter.Parser(parser).parse(source)
            return list(
                python_items(
                    nodes=tree.root_node.named_children,
                    source=source,
                    path=name,
                )
            )
        case parser if parser is BASH:
            tree = tree_sitter.Parser(parser).parse(source)
            return list(
                shell_items(
                    nodes=tree.root_node.named_children,
                    source=source,
                    path=name,
                )
            )
        case parser:
            tree = tree_sitter.Parser(parser).parse(source)
            return list(
                items(
                    nodes=tree.root_node.named_children,
                    source=source,
                    path=name,
                )
            )


def merge_constants(scanned):
    """Consecutive const, static and assignment items as one item, so each costs one review together."""
    merged = []
    for item in scanned:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.file == item.file
            and previous.context == item.context
            and previous.kind in MERGED
            and item.kind in MERGED
            and item.start - previous.end <= 2
        ):
            gap = "\n" * (item.start - previous.end)
            merged[-1] = previous.model_copy(
                update={
                    "end": item.end,
                    "name": f"{previous.name}, {item.name}",
                    "text": previous.text + gap + item.text,
                }
            )
        else:
            merged.append(item)
    return merged


def test_cutoff(source, marker):
    """The line number of the first line that is `marker`, after the last line when there is none."""
    lines = source.decode(errors="replace").splitlines()
    return next(
        (
            n
            for n, line in enumerate(lines, 1)
            if marker is not None and line == marker
        ),
        len(lines) + 1,
    )


async def changed_items(root, pathspecs, base, context, sets):
    """The hunks changed since `base`'s merge-base with HEAD, each with `context` as git diff
    options for its context lines, and the untracked files."""
    if base.startswith("-"):
        raise UserError(f"base {base!r} is not a revision")
    merge_base = await git.merge_base(root, base)
    diff = await git.diff(
        root=root, revision=merge_base, context=context, pathspecs=pathspecs
    )
    changed = []
    for patched in unidiff.PatchSet(diff):
        if patched.is_removed_file or patched.is_binary_file:
            continue
        name = patched.path
        # A submodule's diff names its directory.
        if not (root / name).is_file() or not inside(root, root / name):
            continue
        source = (root / name).read_bytes()
        language = language_of(name=name, source=source, sets=sets)
        cutoff = test_cutoff(source, language.test_marker)
        for hunk in patched:
            lines = [
                line
                for line in hunk
                if (line.is_added or line.is_context)
                and line.target_line_no < cutoff
            ]
            if not any(line.is_added for line in lines):
                continue
            header = (
                f"@@ -{hunk.source_start},{hunk.source_length} "
                f"+{hunk.target_start},{hunk.target_length} @@ {hunk.section_header}"
            )
            changed.append(
                Item(
                    file=name,
                    start=hunk.target_start,
                    end=lines[-1].target_line_no,
                    name=hunk.section_header or ItemKind.HUNK,
                    kind=ItemKind.HUNK,
                    context="",
                    text="",
                    sets=language.sets,
                    hunks=[
                        DiffHunk(
                            header=header.rstrip(),
                            lines=[
                                f"{line.line_type}{line.value.removesuffix('\n')}"
                                for line in lines
                            ],
                        )
                    ],
                )
            )
    return changed, await git.untracked_files(root, pathspecs)


DEFAULT_CONTEXT = 20


def added_everywhere(item):
    """`item` with every context line of its hunks marked as added, so it is judged too."""
    return item.model_copy(
        update={
            "hunks": [
                hunk.model_copy(
                    update={"lines": [f"+{line[1:]}" for line in hunk.lines]}
                )
                for hunk in item.hunks
            ]
        }
    )


def file_diffs(hunks):
    """One item per file holding all of the file's hunks."""
    by_file = {}
    for hunk in hunks:
        by_file.setdefault(hunk.file, []).append(hunk)
    return [
        Item(
            file=name,
            start=file_hunks[0].start,
            end=file_hunks[-1].end,
            name=ItemKind.FILE,
            kind=ItemKind.FILE,
            context="",
            text="",
            sets=file_hunks[0].sets,
            hunks=[hunk for item in file_hunks for hunk in item.hunks],
        )
        for name, file_hunks in by_file.items()
    ]


def change_items(hunks, untracked, evaluations, all_added, sets):
    """The items that evaluate the change's `hunks` as each of `evaluations` says. An untracked
    file is all added, so its diff is the whole file, and its hunks are its top-level items."""
    if all_added:
        hunks = [added_everywhere(hunk) for hunk in hunks]
    new_files = []
    for name, source in untracked.items():
        language = language_of(name=name, source=source, sets=sets)
        lines = source.decode(errors="replace").splitlines()
        lines = lines[: test_cutoff(source, language.test_marker) - 1]
        new_files.append(
            Item(
                file=name,
                start=1,
                end=max(len(lines), 1),
                name=ItemKind.FILE,
                kind=ItemKind.FILE,
                context="",
                text="\n".join(lines),
                sets=language.sets,
            )
        )

    def evaluated(items, evaluation):
        return [
            item.model_copy(update={"evaluated": evaluation}) for item in items
        ]

    scanned = []
    if Evaluation.DIFF in evaluations:
        files = file_diffs(hunks) + new_files
        if files:
            scanned.append(
                Item(
                    file=".",
                    start=0,
                    end=0,
                    name=ItemKind.DIFF,
                    kind=ItemKind.DIFF,
                    context="",
                    text="",
                    state="\n".join(item.diff for item in files),
                    sets=tuple(
                        dict.fromkeys(s for item in files for s in item.sets)
                    ),
                    evaluated=Evaluation.DIFF,
                )
            )
    if Evaluation.FILE in evaluations:
        scanned += evaluated(file_diffs(hunks) + new_files, Evaluation.FILE)
    if Evaluation.HUNK in evaluations:
        new = [
            item
            for name, source in untracked.items()
            for item in file_items(
                name=name,
                source=source,
                language=language_of(name=name, source=source, sets=sets),
            )
        ]
        scanned += evaluated(
            [
                item.model_copy(update={"headless": True})
                for item in hunks + merge_constants(new)
            ],
            Evaluation.HUNK,
        )
    return scanned


def all_files(root, targets):
    """The files under each of `targets`, or each target itself, by path from `root`. Symlinks
    under a target are left out, so no file outside `root` is reached."""
    stack = []
    files = set()
    for target in targets:
        if target.is_file():
            files.add(str(target.relative_to(root)))
        else:
            stack.append(target)
    while stack:
        for path in stack.pop().iterdir():
            if path.is_symlink():
                continue
            if path.is_dir():
                stack.append(path)
            elif path.is_file():
                files.add(str(path.relative_to(root)))
    return sorted(files)


def findings(item, root, answers, rule_set):
    """The rules of `rule_set` that jev's answers for `item` meet its error threshold on, as findings."""
    parsed = read_set(rule_set)
    for position, rule in enumerate(parsed.rules, 1):
        probability = answers.get(jev_review.jev_ask.question_name(position))
        if probability is None or probability < parsed.error_threshold:
            continue
        yield Finding(
            file=str(root / item.file),
            start=item.start,
            end=item.end,
            item=item.name,
            rule=f"{rule_set.name}#{position}",
            probability=round(probability, 2),
            flags=rule.flags,
            violation=" ".join(rule.bad),
            accepted=" ".join(rule.good),
            evaluated=item.evaluated,
        )


async def ask(item, rule_set, gate, verbose, settings):
    state = item.state if item.state is not None else item.diff
    async with gate:
        try:
            request = jev_review.jev_ask.build_request(
                questions=jev_review.jev_ask.load_questions(rule_set),
                state=state,
                settings=settings,
            )
        except jev_review.jev_ask.UserError as e:
            raise AskFailed(item, str(e))
        except jev_review.jev_ask.Rejected as e:
            raise AskFailed(
                item, "\n".join(jev_review.jev_ask.rejected_lines(e))
            )
        if verbose:
            print(
                json_dumps(
                    {
                        "file": item.file,
                        "start": item.start,
                        "item": item.name,
                        "evaluated": item.evaluated,
                        "request": request.model_dump(
                            mode="json", exclude_none=True
                        ),
                    }
                ),
                file=sys.stderr,
            )
        try:
            answers = await jev_review.jev_ask.ask(request, settings)
        except jev_review.jev_ask.UserError as e:
            raise AskFailed(item, str(e))
    return Answers.model_validate(answers).root


async def scan_items(
    rules,
    root=None,
    paths=(),
    extra_rules=(),
    rule_set=None,
    base=None,
    evaluate=None,
    context=None,
    all_added=False,
):
    """The resolved root, and the items of it to ask jev about, as main's
    flags of the same names select them."""
    evaluations = evaluate or [Evaluation.HUNK]

    root = (await git.current_repository() if root is None else root).resolve()
    if not root.is_dir():
        raise UserError(f"--root {root} is not a directory")
    sets = sets_under(
        tuple(
            {
                found.path: found
                for directory in (rules, *extra_rules)
                for found in rule_sets(directory.resolve())
            }.values()
        ),
        root,
    )
    targets = []
    for path in paths:
        if path.is_absolute():
            raise UserError(
                f"{path} is absolute; give PATHs relative to --root"
            )
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise UserError(f"{path} is outside --root {root}")
        if not target.is_file() and not target.is_dir():
            raise UserError(
                f"{path} is neither a directory nor a file in {root}"
            )
        targets.append(target)
    targets = targets or [root]
    pathspecs = [str(target.relative_to(root)) for target in targets]
    # Files named on the command line are reviewed whatever their language.
    named = {
        str(target.relative_to(root)) for target in targets if target.is_file()
    }

    if base is None and (
        evaluate is not None or context is not None or all_added
    ):
        raise UserError("--evaluate, --context and --all-added need --base")
    if not await git.in_repository(root):
        # Outside a repository every file is new: reviewed whole, or as added with --base.
        hunks, whole = [], all_files(root, targets)
    elif base is None:
        whole = await git.tracked_files(root, pathspecs)
        if ignored := sorted(named - set(whole)):
            raise UserError(f"{', '.join(ignored)} ignored by git")
    else:
        lines = DEFAULT_CONTEXT if context is None else context
        hunks, whole = await changed_items(
            root=root,
            pathspecs=pathspecs,
            base=base,
            context=(f"-U{lines}",),
            sets=sets,
        )
    sources = {}
    for name in whole:
        # git lists an untracked nested repository as its directory. A symlink out of the root
        # would send a file outside it to jev.
        if not (root / name).is_file() or not inside(root, root / name):
            continue
        source = (root / name).read_bytes()
        try:
            source.decode()
        except UnicodeDecodeError:
            # Not text, so nothing for the rule sets to judge.
            continue
        if (
            base is None
            and name not in named
            and not in_whole_scan(name, source)
        ):
            continue
        sources[name] = source
    if base is None:
        scanned = merge_constants(
            [
                item
                for name, source in sources.items()
                for item in file_items(
                    name=name,
                    source=source,
                    language=language_of(name=name, source=source, sets=sets),
                )
            ]
        )
    else:
        scanned = change_items(
            hunks=hunks,
            untracked=sources,
            evaluations=evaluations,
            all_added=all_added,
            sets=sets,
        )
    scanned = [
        item.model_copy(
            update={"sets": (rule_set,) if rule_set else item.sets}
        )
        for item in scanned
        if rule_set or item.sets
    ]
    return root, scanned


async def review(scanned, root, concurrency, verbose):
    """The findings jev reports on scanned, and the requests that failed."""
    asked = [(item, s) for item in scanned for s in item.sets]
    gate = asyncio.Semaphore(concurrency)
    settings = jev_review.jev_ask.load_settings()
    results = await asyncio.gather(
        *(
            ask(
                item=item,
                rule_set=s,
                gate=gate,
                verbose=verbose,
                settings=settings,
            )
            for item, s in asked
        ),
        return_exceptions=True,
    )
    if asked and all(isinstance(result, AskFailed) for result in results):
        raise UserError(f"every jev request failed; the first: {results[0]}")
    found = []
    failures = []
    # A rule two of an item's sets both ask, word for word, is reported once, from the first set.
    reported = set()
    for (item, rule_set), result in zip(asked, results):
        if isinstance(result, AskFailed):
            failures.append(f"{rule_set.name}: {result}")
            continue
        if isinstance(result, BaseException):
            raise result
        for finding in findings(
            item=item, root=root, answers=result, rule_set=rule_set
        ):
            if (id(item), finding.flags) in reported:
                continue
            reported.add((id(item), finding.flags))
            found.append(finding)
    return without_file_repeats(found), failures


def summary_line(scanned, root, found, failures):
    return (
        f"{len(scanned)} items reviewed in {root}, {len(found)} findings, "
        f"{len(failures)} failures"
    )


def without_file_repeats(found):
    """`found` without each file item's finding whose rule also fired on a hunk of the same file,
    as the hunk's finding names the lines."""
    in_hunks = {
        (finding.file, finding.flags)
        for finding in found
        if finding.evaluated == Evaluation.HUNK
    }
    return [
        finding
        for finding in found
        if finding.evaluated != Evaluation.FILE
        or (finding.file, finding.flags) not in in_hunks
    ]

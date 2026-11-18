"""The contents of a rule set file, as typed models."""

import enum
import typing

import pathspec.patterns.gitignore.spec
import pydantic

type GitIgnorePattern = typing.Annotated[
    pathspec.patterns.gitignore.spec.GitIgnoreSpecPattern,
    pydantic.PlainValidator(
        pathspec.patterns.gitignore.spec.GitIgnoreSpecPattern
    ),
    pydantic.PlainSerializer(lambda pattern: pattern.pattern, return_type=str),
]


class Language(enum.StrEnum):
    RUST = "rust"
    PYTHON = "python"
    SH = "sh"
    JUST = "just"
    MARKDOWN = "markdown"


class When(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    language: tuple[Language, ...] = ()
    matches: tuple[GitIgnorePattern, ...] = ()
    exists: tuple[GitIgnorePattern, ...] = ()


class Rule(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    flags: str
    good: tuple[str, ...] = ()
    bad: tuple[str, ...] = ()


class RuleSet(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid", frozen=True)

    description: str | None = None
    when: tuple[When, ...] | None = None
    error_threshold: typing.Annotated[float, pydantic.Field(ge=0.0, le=1.0)]
    rules: tuple[Rule, ...]

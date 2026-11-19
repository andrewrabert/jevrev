"""Build the request asking TypeSafe's Jev whether a state breaks each rule
of a rule set."""

import json
import pathlib
import typing

import pydantic
import yaml

import jevrev.rules

# Jev's context window in tokens, estimated as a payload's bytes over 4.
_MAX_CONTEXT_TOKENS = 32_000


class UserError(Exception):
    pass


class Rejected(Exception):
    """The pydantic.ValidationError of a request that would fail or waste a call."""


def _parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise UserError(
            f"input is neither valid JSON nor valid YAML: {e}"
        ) from e


# What every noul tells jev to judge.
_TASK = 'Judge only the lines that begin with "+" in this diff; other lines are context.'


type _NonBlank = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"\S")
]


class Instructions(pydantic.BaseModel):
    task: _NonBlank
    flags: str


class Criteria(pydantic.BaseModel):
    true: str
    false: str


class Noul(pydantic.BaseModel):
    type: typing.Literal["noul"] = "noul"
    instructions: Instructions
    criteria: Criteria | None = None


class Request(pydantic.BaseModel):
    """A request jev can answer: its state and questions are not empty, and
    it fits jev's context window."""

    model: str
    state: _NonBlank
    questions: typing.Annotated[dict[str, Noul], pydantic.Field(min_length=1)]

    @pydantic.model_validator(mode="after")
    def _fits_context(self):
        tokens = len(self.to_json().encode()) // 4 + 1
        if tokens > _MAX_CONTEXT_TOKENS:
            raise ValueError(
                f"roughly {tokens} estimated tokens cannot fit the "
                f"{_MAX_CONTEXT_TOKENS}-token limit; split the state, or drop "
                "rules that do not need the full context"
            )
        return self

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


def _noul(rule: jevrev.rules.Rule) -> Noul:
    """The noul jev is asked: true when the code is what the rule flags."""
    return Noul(
        instructions=Instructions(task=_TASK, flags=rule.flags),
        criteria=Criteria(true=" ".join(rule.bad), false=" ".join(rule.good))
        if rule.good or rule.bad
        else None,
    )


def question_name(position):
    """The name jev gets for the rule at `position`, from 1, in its rule set."""
    return f"q{position}"


def _parse_rules(text):
    """The nouls jev is asked for a rule set, each named by question_name."""
    try:
        rule_set = jevrev.rules.RuleSet.model_validate(_parse_value(text))
    except pydantic.ValidationError as e:
        raise UserError(str(e)) from e
    return {
        question_name(position): _noul(rule)
        for position, rule in enumerate(rule_set.rules, 1)
    }


def load_questions(path: pathlib.Path) -> dict[str, Noul]:
    """The nouls jev is asked for the rule set file at path."""
    try:
        text = path.read_text()
    except OSError as e:
        raise UserError(f"failed to read {path}: {e.strerror}") from e
    return _parse_rules(text)


def build_request(
    questions: dict[str, Noul], state: str, model: str
) -> Request:
    """The request asking jev questions about state; Rejected when it would
    fail or waste a call."""
    try:
        return Request(model=model, state=state, questions=questions)
    except pydantic.ValidationError as e:
        raise Rejected(e) from e


def rejected_lines(rejected: Rejected) -> list[str]:
    """One line per problem of a rejected request, then a closing line."""
    (error,) = rejected.args
    problems = error.errors(include_url=False)
    return [
        *(
            f"error: {'.'.join(map(str, problem['loc'])) or 'request'}: "
            f"{problem['msg']}"
            for problem in problems
        ),
        f"{len(problems)} problem(s) would cause a failed or wasted call; fix them",
    ]

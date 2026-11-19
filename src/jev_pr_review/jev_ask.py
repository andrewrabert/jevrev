"""Ask TypeSafe's Jev whether the text on stdin breaks each rule of a rule set, and print one answer per rule as JSON."""

import argparse
import asyncio
import json
import os
import pathlib
import sys
import typing

import pydantic
import pydantic_settings
import typesafe_sdk
import yaml
import yarl

import jev_rules.models

# Jev's context window in tokens, estimated as a payload's bytes over 4.
MAX_CONTEXT_TOKENS = 32_000

# Exit status when the request would fail or waste a call, so nothing was sent.
REJECTED = 2

EPILOG = """\
examples:
  git diff -W | jev-ask -r standards.yaml
  jev-ask --dry-run -r standards.yaml < /dev/null

settings, read from the environment, then TYPESAFE_ENV_FILE:
  TYPESAFE_API_KEY        API key
  TYPESAFE_DEFAULT_MODEL  model; default jev-latest
  TYPESAFE_BASE_URL       API root; default https://api.typesafe.ai
  TYPESAFE_TIMEOUT        seconds per request; default 90
  TYPESAFE_ENV_FILE       dotenv file holding the settings above; read from the environment only
"""


class UserError(Exception):
    pass


class Rejected(Exception):
    """The pydantic.ValidationError of a request that would fail or waste a call."""


def absolute_url(url: yarl.URL) -> yarl.URL:
    if not url.absolute:
        raise ValueError(f"{url} is not an absolute URL")
    return url


type Url = typing.Annotated[
    yarl.URL,
    pydantic.PlainValidator(yarl.URL),
    pydantic.AfterValidator(absolute_url),
    pydantic.PlainSerializer(str),
]


class Settings(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="TYPESAFE_",
        env_file=os.environ.get("TYPESAFE_ENV_FILE"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: pydantic.SecretStr | None = None
    base_url: Url = yarl.URL(typesafe_sdk.constants.DEFAULT_BASE_URL)
    default_model: str = typesafe_sdk.constants.DEFAULT_MODEL
    timeout: pydantic.PositiveFloat = pydantic.Field(
        90.0,
        description="Seconds per request; the SDK's own default of 10 cuts "
        "off long states.",
    )


def json_dumps(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse_value(text):
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
TASK = 'Judge only the lines that begin with "+" in this diff; other lines are context.'


type NonBlank = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"\S")
]


class Instructions(pydantic.BaseModel):
    task: NonBlank
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
    state: NonBlank
    questions: typing.Annotated[dict[str, Noul], pydantic.Field(min_length=1)]

    @pydantic.model_validator(mode="after")
    def _fits_context(self):
        tokens = len(self.to_json().encode()) // 4 + 1
        if tokens > MAX_CONTEXT_TOKENS:
            raise ValueError(
                f"roughly {tokens} estimated tokens cannot fit the "
                f"{MAX_CONTEXT_TOKENS}-token limit; split the state, or drop "
                "rules that do not need the full context"
            )
        return self

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)


def noul(rule: jev_rules.models.Rule) -> Noul:
    """The noul jev is asked: true when the code is what the rule flags."""
    return Noul(
        instructions=Instructions(task=TASK, flags=rule.flags),
        criteria=Criteria(true=" ".join(rule.bad), false=" ".join(rule.good))
        if rule.good or rule.bad
        else None,
    )


def question_name(position):
    """The name jev gets for the rule at `position`, from 1, in its rule set."""
    return f"q{position}"


def parse_rules(text):
    """The nouls jev is asked for a rule set, each named by question_name."""
    try:
        rule_set = jev_rules.models.RuleSet.model_validate(parse_value(text))
    except pydantic.ValidationError as e:
        raise UserError(str(e)) from e
    return {
        question_name(position): noul(rule)
        for position, rule in enumerate(rule_set.rules, 1)
    }


def summary(response):
    values = {}
    for name, answer in sorted(response.answers.items()):
        match answer:
            case typesafe_sdk.NoulAnswer():
                values[name] = answer.noul
            case typesafe_sdk.ChoiceAnswer():
                values[name] = answer.choice
            case typesafe_sdk.ScoreAnswer():
                values[name] = answer.score
    return values


def load_questions(path: pathlib.Path) -> dict[str, Noul]:
    """The nouls jev is asked for the rule set file at path."""
    try:
        text = path.read_text()
    except OSError as e:
        raise UserError(f"failed to read {path}: {e.strerror}") from e
    return parse_rules(text)


def load_settings() -> Settings:
    """The settings read from the environment, then TYPESAFE_ENV_FILE."""
    try:
        return Settings()
    except pydantic.ValidationError as e:
        raise UserError(f"invalid settings: {e}") from e


def build_request(
    questions: dict[str, Noul], state: str, settings: Settings
) -> Request:
    """The request asking jev questions about state; Rejected when it would
    fail or waste a call."""
    try:
        return Request(
            model=settings.default_model, state=state, questions=questions
        )
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


async def ask(request: Request, settings: Settings) -> dict:
    """Jev's answer to each question of request, by name."""
    if settings.api_key is None:
        raise UserError(
            "TYPESAFE_API_KEY is not set in the environment or TYPESAFE_ENV_FILE"
        )
    try:
        async with typesafe_sdk.AsyncTypeSafeClient(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            model=request.model,
            timeout=settings.timeout,
            # LiteLLM's pass-through fails on zstd request bodies, which the SDK sends from 1 KiB up.
            compression=False,
        ) as client:
            response = await client.system_one(
                state=request.state,
                questions=request.model_dump(mode="json", exclude_none=True)[
                    "questions"
                ],
            )
    except typesafe_sdk.TypeSafeError as e:
        raise UserError(str(e)) from e
    return summary(response)


async def main():
    parser = argparse.ArgumentParser(
        prog="jev-ask",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-r",
        "--rules",
        type=pathlib.Path,
        required=True,
        help="rule set file (YAML or JSON)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request that would be sent, and exit without sending it",
    )
    args = parser.parse_args()
    questions = load_questions(args.rules)
    if sys.stdin.isatty():
        raise UserError("no input given, and stdin is a terminal")
    state = sys.stdin.read()
    settings = load_settings()
    request = build_request(
        questions=questions, state=state, settings=settings
    )
    if args.dry_run:
        print(request.to_json())
        return
    print(json_dumps(await ask(request, settings)))


def cli():
    try:
        sys.exit(asyncio.run(main()))
    except UserError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Rejected as e:
        for line in rejected_lines(e):
            print(line, file=sys.stderr)
        sys.exit(REJECTED)


if __name__ == "__main__":
    cli()

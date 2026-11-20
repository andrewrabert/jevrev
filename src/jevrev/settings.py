"""jevrev's settings, read from the environment, then the dotenv file --env-file or
JEVREV_ENV_FILE names; a variable the environment sets wins over the file."""

import os
import pathlib
import typing

import dotenv.parser
import pydantic
import pydantic_settings
import yarl

import jevrev.github.repo
import jevrev.jev.api

ENV_FILE = "JEVREV_ENV_FILE"


class UserError(Exception):
    pass


def _absolute_url(url: yarl.URL) -> yarl.URL:
    if not url.absolute:
        raise ValueError(f"{url} is not an absolute URL")
    return url


type _Url = typing.Annotated[
    yarl.URL,
    pydantic.PlainValidator(yarl.URL),
    pydantic.AfterValidator(_absolute_url),
    pydantic.PlainSerializer(str),
]


def path_list(value: str) -> list[pathlib.Path]:
    """The directories of value, in the system's path-list syntax, empty entries left out."""
    return [pathlib.Path(entry) for entry in value.split(os.pathsep) if entry]


def _path_list(value: object) -> object:
    if isinstance(value, str):
        return path_list(value)
    return value


type _PathList = typing.Annotated[
    list[pathlib.Path],
    pydantic_settings.NoDecode,
    pydantic.BeforeValidator(_path_list),
]

type _Secret = typing.Annotated[
    pydantic.SecretStr, pydantic.StringConstraints(min_length=1)
]


class Settings(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    rules: _PathList | None = pydantic.Field(
        None,
        validation_alias="JEVREV_RULES",
        description="Directories of rule sets jev-scan reviews against, in the system's "
        "path-list syntax, used when no --rules is given.",
    )
    api_key: _Secret | None = pydantic.Field(
        None, validation_alias="TYPESAFE_API_KEY"
    )
    base_url: _Url = pydantic.Field(
        jevrev.jev.api.DEFAULT_BASE_URL, validation_alias="TYPESAFE_BASE_URL"
    )
    default_model: typing.Annotated[
        str, pydantic.StringConstraints(min_length=1)
    ] = pydantic.Field(
        jevrev.jev.api.DEFAULT_MODEL, validation_alias="TYPESAFE_DEFAULT_MODEL"
    )
    timeout: pydantic.PositiveFloat = pydantic.Field(
        90.0,
        validation_alias="TYPESAFE_TIMEOUT",
        description="Seconds each connection and read may take; inf for no "
        "limit; the SDK's own default of 10 cuts off long states.",
    )
    github_host: typing.Annotated[
        str,
        pydantic.StringConstraints(pattern=jevrev.github.repo.HOST.pattern),
    ] = pydantic.Field(
        jevrev.github.repo.DEFAULT_HOST, validation_alias="GITHUB_HOST"
    )
    github_token: _Secret | None = pydantic.Field(
        None, validation_alias="GITHUB_TOKEN"
    )


def _check_env_file(path: pathlib.Path):
    """Fail on a missing, unreadable or malformed env file, which pydantic-settings would skip
    or read in part."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise UserError(f"{path}: {e}") from e
    for binding in dotenv.parser.parse_stream(_Stream(text)):
        if binding.error:
            raise UserError(
                f"{path}: line {binding.original.line}: not NAME=VALUE"
            )


class _Stream:
    """text as the stream dotenv's parser reads."""

    def __init__(self, text: str):
        self.text = text

    def read(self) -> str:
        return self.text


def load_settings(env_file: pathlib.Path | None) -> Settings:
    """The settings, with env_file, else the file JEVREV_ENV_FILE names, if either is set."""
    if env_file is None and (named := os.environ.get(ENV_FILE)):
        env_file = pathlib.Path(named)
    if env_file is not None:
        _check_env_file(env_file)
    try:
        return Settings(_env_file=env_file)
    except pydantic.ValidationError as e:
        raise UserError(f"invalid settings: {e}") from e

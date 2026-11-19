"""jevrev's settings, read from the environment, then TYPESAFE_ENV_FILE."""

import os
import typing

import pydantic
import pydantic_settings
import yarl

import jevrev.jev.api


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


class Settings(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="TYPESAFE_",
        env_file=os.environ.get("TYPESAFE_ENV_FILE"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: pydantic.SecretStr | None = None
    base_url: _Url = jevrev.jev.api.DEFAULT_BASE_URL
    default_model: str = jevrev.jev.api.DEFAULT_MODEL
    timeout: pydantic.PositiveFloat = pydantic.Field(
        90.0,
        description="Seconds per request; the SDK's own default of 10 cuts "
        "off long states.",
    )


def load_settings() -> Settings:
    """The settings read from the environment, then TYPESAFE_ENV_FILE."""
    try:
        return Settings()
    except pydantic.ValidationError as e:
        raise UserError(f"invalid settings: {e}") from e

import re
import string
import typing

import pydantic

DEFAULT_HOST = "github.com"

# A GitHub host as gh takes it, such as github.com or a GitHub Enterprise
# Server host, with an optional port. Never . or .., so it is one path
# component.
HOST = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$"
)

type _GithubHost = typing.Annotated[
    str, pydantic.StringConstraints(pattern=HOST.pattern)
]


_GITHUB_NAME_CHARS = frozenset(string.ascii_letters + string.digits + ".-_")
# Path components, not names.
_GITHUB_NAME_INVALID = frozenset((".", ".."))


def _valid_github_name(value: str) -> bool:
    return (
        bool(value)
        and not set(value) - _GITHUB_NAME_CHARS
        and value not in _GITHUB_NAME_INVALID
    )


@pydantic.dataclasses.dataclass(frozen=True)
class GithubRepoName:
    """A repo's ORG/NAME."""

    # Organization or user that owns the repo.
    org: str
    # Repository name.
    name: str

    @classmethod
    def parse(cls, value: str) -> "GithubRepoName":
        org, slash, name = value.partition("/")
        if not slash:
            raise ValueError(f"{value}: expected ORG/NAME")
        if not _valid_github_name(org):
            raise ValueError(f"{value}: org: invalid github name")
        if not _valid_github_name(name):
            raise ValueError(f"{value}: name: invalid github name")
        return cls(org=org, name=name)

    def __str__(self) -> str:
        return f"{self.org}/{self.name}"


def _repo_name(value: object) -> object:
    if isinstance(value, str):
        return GithubRepoName.parse(value)
    return value


# Serialized as ORG/NAME.
type _RepoName = typing.Annotated[
    GithubRepoName,
    pydantic.BeforeValidator(_repo_name),
    pydantic.PlainSerializer(str, return_type=str),
]


class GithubRepo(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    host: _GithubHost = pydantic.Field(description="GitHub host")
    name: _RepoName = pydantic.Field(description="The repo's ORG/NAME")

import string
import typing

import pydantic

# A GitHub host as gh takes it, such as github.com or a GitHub Enterprise
# Server host, with an optional port. Never . or .., so it is one path
# component.
type _GithubHost = typing.Annotated[
    str,
    pydantic.StringConstraints(
        pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$"
    ),
]


_GITHUB_NAME_CHARS = frozenset(string.ascii_letters + string.digits + ".-_")
# Path components, not names.
_GITHUB_NAME_INVALID = frozenset((".", ".."))


def _check_github_name(value: str) -> str:
    if set(value) - _GITHUB_NAME_CHARS or value in _GITHUB_NAME_INVALID:
        raise ValueError("invalid github name")
    return value


type _GithubOrgName = typing.Annotated[
    str,
    pydantic.StringConstraints(min_length=1),
    pydantic.AfterValidator(_check_github_name),
]

type _GithubRepoName = typing.Annotated[
    str,
    pydantic.StringConstraints(min_length=1),
    pydantic.AfterValidator(_check_github_name),
]


class GithubRepo(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    host: _GithubHost = pydantic.Field(description="GitHub host")
    org: _GithubOrgName = pydantic.Field(
        description="Organization or user that owns the repo"
    )
    name: _GithubRepoName = pydantic.Field(description="Repository name")

import enum
import typing

import pydantic
import yarl

import jevrev.git

type GithubPRNumber = typing.Annotated[int, pydantic.Field(gt=0)]

type _Url = typing.Annotated[
    yarl.URL,
    pydantic.PlainValidator(yarl.URL),
    pydantic.PlainSerializer(str),
]


class User(pydantic.BaseModel):
    login: str


class PullRequest(pydantic.BaseModel):
    """A pull request as GET /repos/O/R/pulls/N gives it: its author, its head, and the head of
    the branch it merges into."""

    author: User = pydantic.Field(validation_alias="user")
    head: jevrev.git.GitObjectId = pydantic.Field(
        validation_alias=pydantic.AliasPath("head", "sha")
    )
    target: jevrev.git.GitObjectId = pydantic.Field(
        validation_alias=pydantic.AliasPath("base", "sha")
    )


class GithubPullRequest(jevrev.git.GitDiff):
    """A pull request's changes: base is merge-base(target, head), head is the PR's head."""

    number: GithubPRNumber


class LineReviewComment(pydantic.BaseModel):
    """A review comment to post on lines start_line to line of the head."""

    path: str
    body: str
    line: int
    start_line: int | None = None
    side: typing.Literal["RIGHT"] = "RIGHT"


class FileReviewComment(pydantic.BaseModel):
    """A review comment to post on a file of the head."""

    path: str
    body: str


type ReviewComment = LineReviewComment | FileReviewComment


class ReviewEvent(enum.StrEnum):
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class NewReview(pydantic.BaseModel):
    """A review to post: its body and its comments, on commit_id, submitted
    as event."""

    commit_id: jevrev.git.GitObjectId
    event: ReviewEvent
    body: str
    comments: list[ReviewComment]


class PendingReviewInput(pydantic.BaseModel):
    """The REST body that starts a pending review: the API takes only line
    comments here, so file comments are added to it as threads."""

    commit_id: jevrev.git.GitObjectId
    body: str
    comments: list[LineReviewComment]


class PendingReview(pydantic.BaseModel):
    id: int
    node_id: str


class ReviewThread(pydantic.BaseModel):
    id: str


class AddedReviewThread(pydantic.BaseModel):
    # GitHub answers null, and no error, when it does not add the thread,
    # such as for a path not in the pull request; that fails validation.
    thread: ReviewThread


class AddReviewThreadData(pydantic.BaseModel):
    addPullRequestReviewThread: AddedReviewThread


class AddReviewThreadResponse(pydantic.BaseModel):
    data: AddReviewThreadData


class AddFileThreadVariables(pydantic.BaseModel):
    review: str
    path: str
    body: str


class GraphqlRequest(pydantic.BaseModel):
    query: str
    variables: AddFileThreadVariables


class ReviewEventInput(pydantic.BaseModel):
    """The REST body that submits a pending review."""

    event: ReviewEvent


class Review(pydantic.BaseModel):
    html_url: _Url

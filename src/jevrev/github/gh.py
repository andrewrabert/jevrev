import collections.abc
import contextlib
import enum
import logging
import pathlib

import yarl

import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.process

_PULL_REQUEST_FIELDS = "author,baseRefName"
# gh's pull request JSON as a jevrev.github.pull_request.PullRequest.
_PULL_REQUEST_JQ = (
    "{author: {login: .author.login}, base_ref_name: .baseRefName}"
)


# Adds a file comment to a pending review; the REST API that creates a
# review takes only line comments.
_ADD_FILE_THREAD = """
mutation($review: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, body: $body, subjectType: FILE
  }) { thread { id } }
}
"""


_LOGGER = logging.getLogger(__name__)


class HttpMethod(enum.StrEnum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


class GithubError(Exception):
    pass


class gh:
    @staticmethod
    async def _run(argv: list[str]) -> bytes:
        try:
            return await jevrev.process.run(argv)
        except jevrev.process.ProcessError as e:
            raise GithubError(str(e)) from e

    @staticmethod
    async def pull_request(
        repo: jevrev.github.repo.GithubRepo,
        pr: jevrev.github.pull_request.GithubPRNumber,
    ) -> jevrev.github.pull_request.PullRequest:
        stdout = await gh._run(
            [
                "gh",
                "pr",
                "view",
                str(pr),
                "--repo",
                f"{repo.host}/{repo.org}/{repo.name}",
                "--json",
                _PULL_REQUEST_FIELDS,
                "--jq",
                _PULL_REQUEST_JQ,
            ]
        )
        return jevrev.github.pull_request.PullRequest.model_validate_json(
            stdout
        )

    @staticmethod
    async def api(
        host: str, method: HttpMethod, path: str, payload: bytes | None = None
    ) -> bytes:
        """Call the API with gh: its response body, or GithubError. GitHub's
        error body, which gh prints on stdout, goes only to the log."""
        argv = [
            "gh",
            "api",
            "--hostname",
            host,
            "--method",
            method,
            *(("--input", "-") if payload is not None else ()),
            path,
        ]
        try:
            return await jevrev.process.run(argv=argv, stdin=payload)
        except jevrev.process.ProcessError as e:
            _LOGGER.error(f"{e}\n{e.stdout.decode().strip()}")
            raise GithubError(str(e)) from e

    @staticmethod
    async def current_user(host: str) -> jevrev.github.pull_request.User:
        """The user gh is logged in as on host."""
        return jevrev.github.pull_request.User.model_validate_json(
            await gh.api(host=host, method=HttpMethod.GET, path="user")
        )

    @staticmethod
    async def post_review(
        repo: jevrev.github.repo.GithubRepo,
        pr: jevrev.github.pull_request.GithubPRNumber,
        review: jevrev.github.pull_request.NewReview,
    ) -> yarl.URL:
        """Post review as one review: started pending with its line
        comments, given its file comments as threads, then submitted. A
        failure deletes the pending review, as GitHub keeps one per user."""
        reviews = f"repos/{repo.org}/{repo.name}/pulls/{pr}/reviews"
        pending = jevrev.github.pull_request.PendingReview.model_validate_json(
            await gh.api(
                host=repo.host,
                method=HttpMethod.POST,
                path=reviews,
                payload=jevrev.github.pull_request.PendingReviewInput(
                    commit_id=review.commit_id,
                    body=review.body,
                    comments=[
                        c
                        for c in review.comments
                        if isinstance(
                            c, jevrev.github.pull_request.LineReviewComment
                        )
                    ],
                )
                .model_dump_json(exclude_none=True)
                .encode(),
            )
        )
        try:
            for comment in review.comments:
                if isinstance(
                    comment, jevrev.github.pull_request.FileReviewComment
                ):
                    jevrev.github.pull_request.AddReviewThreadResponse.model_validate_json(
                        await gh.api(
                            host=repo.host,
                            method=HttpMethod.POST,
                            path="graphql",
                            payload=jevrev.github.pull_request.GraphqlRequest(
                                query=_ADD_FILE_THREAD,
                                variables=jevrev.github.pull_request.AddFileThreadVariables(
                                    review=pending.node_id,
                                    path=comment.path,
                                    body=comment.body,
                                ),
                            )
                            .model_dump_json()
                            .encode(),
                        )
                    )
            submitted = jevrev.github.pull_request.Review.model_validate_json(
                await gh.api(
                    host=repo.host,
                    method=HttpMethod.POST,
                    path=f"{reviews}/{pending.id}/events",
                    payload=jevrev.github.pull_request.ReviewEventInput(
                        event=review.event
                    )
                    .model_dump_json()
                    .encode(),
                )
            )
        except BaseException:
            with contextlib.suppress(GithubError):
                await gh.api(
                    host=repo.host,
                    method=HttpMethod.DELETE,
                    path=f"{reviews}/{pending.id}",
                )
            raise
        return submitted.html_url

    @staticmethod
    async def clone(
        repo: jevrev.github.repo.GithubRepo,
        dest: pathlib.Path,
        git_args: collections.abc.Sequence[str] = (),
    ) -> None:
        """Clone repo to dest, passing git_args to git clone."""
        await gh._run(
            [
                "gh",
                "repo",
                "clone",
                f"{repo.host}/{repo.org}/{repo.name}",
                str(dest),
                "--",
                "--quiet",
                *git_args,
            ]
        )

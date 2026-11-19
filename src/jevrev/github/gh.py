import asyncio
import contextlib
import enum
import logging
import pathlib

import yarl

import jevrev.github.pull_request
import jevrev.github.repo

PULL_REQUEST_FIELDS = "author,baseRefName"
# gh's pull request JSON as a jevrev.github.pull_request.PullRequest.
PULL_REQUEST_JQ = (
    "{author: {login: .author.login}, base_ref_name: .baseRefName}"
)


# Adds a file comment to a pending review; the REST API that creates a
# review takes only line comments.
ADD_FILE_THREAD = """
mutation($review: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, body: $body, subjectType: FILE
  }) { thread { id } }
}
"""


LOGGER = logging.getLogger(__name__)


class HttpMethod(enum.StrEnum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


class GithubError(Exception):
    pass


class gh:
    @staticmethod
    async def pull_request(
        repo: jevrev.github.repo.GithubRepo,
        pr: jevrev.github.pull_request.GithubPRNumber,
    ) -> jevrev.github.pull_request.PullRequest:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            f"{repo.host}/{repo.org}/{repo.name}",
            "--json",
            PULL_REQUEST_FIELDS,
            "--jq",
            PULL_REQUEST_JQ,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise GithubError(f"gh pr view {pr}: {stderr.decode().strip()}")
        return jevrev.github.pull_request.PullRequest.model_validate_json(
            stdout
        )

    @staticmethod
    async def api(
        host: str, method: HttpMethod, path: str, payload: bytes | None = None
    ) -> bytes:
        """Call the API with gh: its response body, or GithubError. GitHub's
        error body, which gh prints on stdout, goes only to the log."""
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "api",
            "--hostname",
            host,
            "--method",
            method,
            *(("--input", "-") if payload is not None else ()),
            path,
            stdin=asyncio.subprocess.DEVNULL
            if payload is None
            else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(payload)
        if proc.returncode != 0:
            LOGGER.error(
                "gh api %s %s: %s %s",
                method,
                path,
                stderr.decode().strip(),
                stdout.decode().strip(),
            )
            raise GithubError(
                f"gh api {method} {path} failed with exit {proc.returncode}"
            )
        return stdout

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
                                query=ADD_FILE_THREAD,
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
        *git_args: str,
    ) -> None:
        """Clone repo to dest, passing git_args to git clone."""
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "repo",
            "clone",
            f"{repo.host}/{repo.org}/{repo.name}",
            str(dest),
            "--",
            "--quiet",
            *git_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise GithubError(f"gh repo clone: {stderr.decode().strip()}")

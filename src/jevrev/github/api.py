"""The GitHub REST and GraphQL APIs jevrev uses, through httpx2, on a host and with a token that
gh's environment variables, or gh itself, give."""

import asyncio
import contextlib
import io
import os
import shutil
import tarfile
import typing

import httpx2
import pydantic
import yarl

import jevrev.files
import jevrev.git
import jevrev.github.pull_request
import jevrev.github.repo
import jevrev.process

_DEFAULT_HOST = "github.com"

# Adds a file comment to a pending review; the REST API that creates a review takes only line
# comments.
_ADD_FILE_THREAD = """
mutation($review: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, body: $body, subjectType: FILE
  }) { thread { id } }
}
"""

# Blob reads a diff has open at once.
_BLOB_CONCURRENCY = 8

# Seconds a request may take; a tarball of a large repository takes long.
_TIMEOUT = 300.0

# Tree entry modes that are a regular or executable file.
_FILE_MODES = frozenset(("100644", "100755"))


class GithubError(Exception):
    pass


def host() -> str:
    """GH_HOST, or github.com when it is unset or empty."""
    return os.environ.get("GH_HOST") or _DEFAULT_HOST


def _token_variables(host: str) -> tuple[str, str]:
    if host == _DEFAULT_HOST:
        return ("GH_TOKEN", "GITHUB_TOKEN")
    return ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


async def token(host: str) -> str:
    """The token for host: from gh's environment variables for it, then from `gh auth token`
    when gh is on PATH."""
    variables = _token_variables(host)
    for variable in variables:
        if value := os.environ.get(variable):
            return value
    if shutil.which("gh") is not None:
        with contextlib.suppress(jevrev.process.ProcessError):
            stdout = await jevrev.process.run(
                ["gh", "auth", "token", "--hostname", host]
            )
            if value := stdout.decode().strip():
                return value
    raise GithubError(
        f"no token for {host}: set {' or '.join(variables)}, or log in with gh"
    )


class _Commit(pydantic.BaseModel):
    sha: jevrev.git.GitObjectId


class _Repository(pydantic.BaseModel):
    default_branch: str


class _Comparison(pydantic.BaseModel):
    merge_base_commit: _Commit


class _TreeEntry(pydantic.BaseModel):
    path: str
    mode: str
    type: str
    sha: jevrev.git.GitObjectId


class _Tree(pydantic.BaseModel):
    tree: list[_TreeEntry]
    truncated: bool


class _GraphqlError(pydantic.BaseModel):
    message: str


class _GraphqlErrors(pydantic.BaseModel):
    errors: list[_GraphqlError] = []


class _Blobs:
    """The files of a tree, each blob read when first read."""

    def __init__(
        self,
        client: "Client",
        repo: jevrev.github.repo.GithubRepo,
        blobs: dict[str, jevrev.git.GitObjectId],
    ):
        self.client = client
        self.repo = repo
        self.blobs = blobs
        self.read_blobs: dict[str, bytes] = {}

    async def paths(self) -> list[str]:
        return sorted(self.blobs)

    async def read(self, path: str) -> bytes:
        if path not in self.read_blobs:
            self.read_blobs[path] = await self.client.blob(
                self.repo, self.blobs[path]
            )
        return self.read_blobs[path]


class Client:
    """The GitHub APIs of host, authenticated with token."""

    def __init__(self, host: str, token: str):
        if host == _DEFAULT_HOST:
            self.rest = yarl.URL("https://api.github.com")
            self.graphql = yarl.URL("https://api.github.com/graphql")
        else:
            self.rest = yarl.URL(f"https://{host}/api/v3")
            self.graphql = yarl.URL(f"https://{host}/api/graphql")
        self.http = httpx2.AsyncClient(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            follow_redirects=True,
            timeout=_TIMEOUT,
        )

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.http.aclose()

    async def _request(
        self,
        method: str,
        url: yarl.URL,
        json: typing.Any = None,
        accept: str | None = None,
    ) -> bytes:
        try:
            response = await self.http.request(
                method,
                str(url),
                json=json,
                headers={"Accept": accept} if accept else None,
            )
        except httpx2.HTTPError as e:
            raise GithubError(f"{method} {url}: {e}") from e
        if response.is_error:
            raise GithubError(
                f"{method} {url}: {response.status_code} {response.text.strip()}"
            )
        return response.content

    def _repo_url(
        self, repo: jevrev.github.repo.GithubRepo, *parts: str
    ) -> yarl.URL:
        url = self.rest / "repos" / repo.org / repo.name
        for part in parts:
            url = url / part
        return url

    async def current_user(self) -> jevrev.github.pull_request.User:
        return jevrev.github.pull_request.User.model_validate_json(
            await self._request("GET", self.rest / "user")
        )

    async def pull_request(
        self,
        repo: jevrev.github.repo.GithubRepo,
        pr: jevrev.github.pull_request.GithubPRNumber,
    ) -> jevrev.github.pull_request.PullRequest:
        return jevrev.github.pull_request.PullRequest.model_validate_json(
            await self._request("GET", self._repo_url(repo, "pulls", str(pr)))
        )

    async def commit(
        self, repo: jevrev.github.repo.GithubRepo, rev: jevrev.git.GitRevision
    ) -> jevrev.git.GitObjectId:
        url = self._repo_url(repo, "commits", rev)
        return _Commit.model_validate_json(await self._request("GET", url)).sha

    async def default_branch_head(
        self, repo: jevrev.github.repo.GithubRepo
    ) -> jevrev.git.GitObjectId:
        """The head of repo's default branch: GET /repos/O/R, then its branch's commit."""
        found = _Repository.model_validate_json(
            await self._request("GET", self._repo_url(repo))
        )
        return await self.commit(repo, found.default_branch)

    async def merge_base(
        self,
        repo: jevrev.github.repo.GithubRepo,
        first: jevrev.git.GitObjectId,
        second: jevrev.git.GitObjectId,
    ) -> jevrev.git.GitObjectId:
        return _Comparison.model_validate_json(
            await self._request(
                "GET", self._repo_url(repo, "compare", f"{first}...{second}")
            )
        ).merge_base_commit.sha

    async def tarball(
        self,
        repo: jevrev.github.repo.GithubRepo,
        commit: jevrev.git.GitObjectId,
    ) -> jevrev.files.Files:
        """The files at commit, from GET /repos/O/R/tarball/COMMIT; symlinks are left out."""
        archive = await self._request(
            "GET", self._repo_url(repo, "tarball", commit)
        )

        def extract() -> dict[str, bytes]:
            contents = {}
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    # Every path is under one top directory, named for the repo and commit.
                    _, _, path = member.name.partition("/")
                    if not member.isfile() or not path:
                        continue
                    handle = tar.extractfile(member)
                    if handle is not None:
                        contents[path] = handle.read()
            return contents

        return jevrev.files.InMemory(await asyncio.to_thread(extract))

    async def _blob_shas(
        self,
        repo: jevrev.github.repo.GithubRepo,
        commit: jevrev.git.GitObjectId,
    ) -> dict[str, jevrev.git.GitObjectId]:
        url = self._repo_url(repo, "git", "trees", commit).with_query(
            recursive="1"
        )
        tree = _Tree.model_validate_json(await self._request("GET", url))
        if tree.truncated:
            raise GithubError(
                f"{repo.org}/{repo.name} has more files at {commit} than GitHub lists in one tree"
            )
        return {
            entry.path: entry.sha
            for entry in tree.tree
            if entry.type == "blob" and entry.mode in _FILE_MODES
        }

    async def blob(
        self, repo: jevrev.github.repo.GithubRepo, sha: jevrev.git.GitObjectId
    ) -> bytes:
        return await self._request(
            "GET",
            self._repo_url(repo, "git", "blobs", sha),
            accept="application/vnd.github.raw+json",
        )

    async def tree(
        self,
        repo: jevrev.github.repo.GithubRepo,
        commit: jevrev.git.GitObjectId,
    ) -> jevrev.files.Files:
        """The files at commit, each read as GET /repos/O/R/git/blobs/SHA when first read; paths
        from GET /repos/O/R/git/trees/COMMIT?recursive=1. GithubError when the tree is
        truncated."""
        return _Blobs(self, repo, await self._blob_shas(repo, commit))

    async def diff(
        self,
        repo: jevrev.github.repo.GithubRepo,
        base: jevrev.git.GitObjectId,
        head: jevrev.git.GitObjectId,
        context: int,
        inter_hunk_context: int,
    ) -> jevrev.git.Diff:
        """The diff from base to head: their trees paired by path, and only the paths whose
        blob differs read and diffed."""
        old, new = await asyncio.gather(
            self._blob_shas(repo, base), self._blob_shas(repo, head)
        )
        changed = sorted(
            path for path, sha in new.items() if old.get(path) != sha
        )
        gate = asyncio.Semaphore(_BLOB_CONCURRENCY)

        async def read(sha: jevrev.git.GitObjectId | None) -> bytes | None:
            if sha is None:
                return None
            async with gate:
                return await self.blob(repo, sha)

        contents = await asyncio.gather(
            *(
                asyncio.gather(read(old.get(path)), read(new[path]))
                for path in changed
            )
        )
        return jevrev.git.git.diff_contents(
            [
                (path, before, after)
                for path, (before, after) in zip(changed, contents)
            ],
            context=context,
            inter_hunk_context=inter_hunk_context,
        )

    async def _graphql(self, request: pydantic.BaseModel) -> bytes:
        response = await self._request(
            "POST", self.graphql, json=request.model_dump(mode="json")
        )
        if errors := _GraphqlErrors.model_validate_json(response).errors:
            raise GithubError(
                "GraphQL: " + "; ".join(error.message for error in errors)
            )
        return response

    async def post_review(
        self,
        repo: jevrev.github.repo.GithubRepo,
        pr: jevrev.github.pull_request.GithubPRNumber,
        review: jevrev.github.pull_request.NewReview,
    ) -> yarl.URL:
        """Post review as one review: started pending with its line comments, given its file
        comments as threads through GraphQL addPullRequestReviewThread, then submitted. A
        failure deletes the pending review, as GitHub keeps one per user."""
        reviews = self._repo_url(repo, "pulls", str(pr), "reviews")
        pending = jevrev.github.pull_request.PendingReview.model_validate_json(
            await self._request(
                "POST",
                reviews,
                json=jevrev.github.pull_request.PendingReviewInput(
                    commit_id=review.commit_id,
                    body=review.body,
                    comments=[
                        c
                        for c in review.comments
                        if isinstance(
                            c, jevrev.github.pull_request.LineReviewComment
                        )
                    ],
                ).model_dump(mode="json", exclude_none=True),
            )
        )
        try:
            for comment in review.comments:
                if isinstance(
                    comment, jevrev.github.pull_request.FileReviewComment
                ):
                    jevrev.github.pull_request.AddReviewThreadResponse.model_validate_json(
                        await self._graphql(
                            jevrev.github.pull_request.GraphqlRequest(
                                query=_ADD_FILE_THREAD,
                                variables=jevrev.github.pull_request.AddFileThreadVariables(
                                    review=pending.node_id,
                                    path=comment.path,
                                    body=comment.body,
                                ),
                            )
                        )
                    )
            submitted = jevrev.github.pull_request.Review.model_validate_json(
                await self._request(
                    "POST",
                    reviews / str(pending.id) / "events",
                    json=jevrev.github.pull_request.ReviewEventInput(
                        event=review.event
                    ).model_dump(mode="json"),
                )
            )
        except BaseException:
            with contextlib.suppress(GithubError):
                await self._request("DELETE", reviews / str(pending.id))
            raise
        return submitted.html_url

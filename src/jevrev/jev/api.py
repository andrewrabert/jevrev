"""TypeSafe's API, through its SDK."""

import pydantic
import typesafe_sdk
import yarl

import jevrev.jev.ask

# Where TypeSafe's API is, and the model it answers with, unless told otherwise.
DEFAULT_BASE_URL = yarl.URL(typesafe_sdk.constants.DEFAULT_BASE_URL)
DEFAULT_MODEL = typesafe_sdk.constants.DEFAULT_MODEL


class UserError(Exception):
    pass


class NoulAnswer(pydantic.BaseModel):
    """Jev's answer to one noul question of a request."""

    model_config = pydantic.ConfigDict(frozen=True)

    # The question's name in the request.
    question: str
    # How likely, from 0 to 1, the question's statement is true.
    probability: float


def _sdk_noul(noul: jevrev.jev.ask.Noul) -> typesafe_sdk.Noul:
    return typesafe_sdk.Noul(
        instructions=noul.instructions.model_dump(mode="json"),
        criteria=None
        if noul.criteria is None
        else typesafe_sdk.NoulCriteria(
            true=noul.criteria.true, false=noul.criteria.false
        ),
    )


class Client:
    """Asks Jev a request's questions on TypeSafe's API at base_url."""

    def __init__(self, api_key: str, base_url: yarl.URL, timeout: float):
        self.api_key = api_key
        self.base_url = base_url
        # Seconds per request.
        self.timeout = timeout

    async def ask(self, request: jevrev.jev.ask.Request) -> list[NoulAnswer]:
        """Jev's answer to each question of request."""
        try:
            async with typesafe_sdk.AsyncTypeSafeClient(
                api_key=self.api_key,
                base_url=str(self.base_url),
                model=request.model,
                timeout=self.timeout,
                # LiteLLM's pass-through fails on zstd request bodies, which the SDK sends from 1 KiB up.
                compression=False,
            ) as client:
                response = await client.system_one(
                    state=request.state,
                    questions={
                        name: _sdk_noul(noul)
                        for name, noul in request.questions.items()
                    },
                )
        except typesafe_sdk.TypeSafeError as e:
            raise UserError(str(e)) from e
        answers = []
        for name, answer in response.answers.items():
            if not isinstance(answer, typesafe_sdk.NoulAnswer):
                raise UserError(
                    f"{name}: jev answered with a {answer.type}, not a noul"
                )
            answers.append(NoulAnswer(question=name, probability=answer.noul))
        return answers

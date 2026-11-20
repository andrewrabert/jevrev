#!/usr/bin/env python3
"""Module docstring."""

import os
from pathlib import Path

# A constant.
LIMIT = 10
OTHER = 20

NAME: str = "x"


# Leading comment
@decorator
def decorated(a, b):
    return a + b


class Base:
    """Doc."""

    attribute = 1

    def method(self):
        pass

    # Comment on second.
    async def second(self):
        await thing()

    value = 2

    @property
    def prop(self):
        return 1


class Empty:
    pass


if __name__ == "__main__":
    main()

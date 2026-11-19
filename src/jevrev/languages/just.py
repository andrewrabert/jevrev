"""just: the whole justfile is one state."""

import jevrev.rules
from jevrev.languages import base


class Just(base.Language):
    name = jevrev.rules.Language.JUST
    suffixes = (".just",)
    file_names = ("justfile", "Justfile", ".justfile")

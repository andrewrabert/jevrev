"""Markdown: the whole file is one state."""

import jevrev.rules
from jevrev.languages import base


class Markdown(base.Language):
    name = jevrev.rules.Language.MARKDOWN
    suffixes = (".md",)

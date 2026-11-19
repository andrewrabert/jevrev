"""The minijinja templates of a review's markdown and of its pull request review, each rendered
with a pydantic model as its only input."""

__all__ = [
    "REVIEW",
    "PR_REVIEW",
    "PR_COMMENT",
]

_FRONT_MATTER = """\
---
{{ model.front_matter }}
---

"""

_TRAILER = """

---
<sub>Posted by an agent</sub>
"""

_FINDING_LINES = """\
{{ f.file }}:{{ f.start }}{% if f.start != f.end %}-{{ f.end }}{% endif %}"""

PR_COMMENT = """\
{{ model.reason }}

<details>
<summary>What this flags</summary>

{{ model.flags }}

</details>"""

PR_REVIEW = (
    """\
Findings: {{ model.found }}
{%- if model.outside is not none %}

In files this pull request does not change:
{%- for rule in model.outside %}

### {{ rule.flags }}
{% for f in rule.findings %}
- `"""
    + _FINDING_LINES
    + """`: \
{{ f.reason or "" }}
{%- endfor %}
{%- endfor %}
{%- if model.left_out %}

And {{ model.left_out }} more, left out for length.
{%- endif %}
{%- endif %}"""
    + _TRAILER
)

REVIEW = (
    _FRONT_MATTER
    + """\
{% for rule in model.rules %}
{%- if not loop.first %}

{% endif -%}
### {{ rule.flags }}
{% for f in rule.findings %}
- """
    + _FINDING_LINES
    + """
{%- endfor %}
{%- endfor %}"""
)

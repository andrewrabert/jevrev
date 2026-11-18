"""The minijinja templates of the reports and pull request comments the agents write, each rendered with a
pydantic model as its only input."""

__all__ = [
    "JEV_INLINE_REVIEW",
    "JEV_INLINE",
    "JEV_INLINE_COMMENT",
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

_JEV_INLINE_MARKER = "<!-- bashkit-agents:jev-code-review:v1 -->"

_FINDING_LINES = """\
{{ f.path }}:{{ f.start }}{% if f.start != f.end %}-{{ f.end }}{% endif %}"""

JEV_INLINE = (
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

JEV_INLINE_COMMENT = (
    _JEV_INLINE_MARKER
    + """

{{ model.reason }}

<details>
<summary>What this flags</summary>

{{ model.flags }}

</details>"""
)

JEV_INLINE_REVIEW = (
    _JEV_INLINE_MARKER
    + """

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

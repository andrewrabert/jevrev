use std::sync::LazyLock;

use crate::store::Finding;

const FRONT_MATTER: &str = "---\n{{ model.front_matter }}\n---\n\n";

const TRAILER: &str = "\n\n---\n<sub>Posted by an agent</sub>\n";

const FINDING_LINES: &str = "{{ f.finding.file }}:{{ f.finding.line_range.start }}{% if f.finding.line_range.start != f.finding.line_range.end %}-{{ f.finding.line_range.end }}{% endif %}";

const PR_COMMENT: &str = "{{ model.reason }}

<details>
<summary>What this flags</summary>

{{ model.flags }}

</details>";

static PR_REVIEW: LazyLock<String> = LazyLock::new(|| {
    format!(
        "Findings: {{{{ model.found }}}}
{{%- if model.outside is not none %}}

In files this pull request does not change:
{{%- for rule in model.outside %}}

### {{{{ rule.flags }}}}
{{% for f in rule.findings %}}
- `{FINDING_LINES}`: {{{{ f.judgment.reason if f.judgment else \"\" }}}}
{{%- endfor %}}
{{%- endfor %}}
{{%- if model.left_out %}}

And {{{{ model.left_out }}}} more, left out for length.
{{%- endif %}}
{{%- endif %}}{TRAILER}"
    )
});

static REVIEW: LazyLock<String> = LazyLock::new(|| {
    format!(
        "{FRONT_MATTER}{{% for rule in model.rules %}}
{{%- if not loop.first %}}

{{% endif -%}}
### {{{{ rule.flags }}}}
{{% for f in rule.findings %}}
- {FINDING_LINES}
{{%- endfor %}}
{{%- endfor %}}"
    )
});

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Template {
    Review,
    PrReview,
    PrComment,
}

impl Template {
    fn name(self) -> &'static str {
        match self {
            Template::Review => "review.md",
            Template::PrReview => "pr_review.md",
            Template::PrComment => "pr_comment.md",
        }
    }
}

static ENVIRONMENT: LazyLock<minijinja::Environment<'static>> = LazyLock::new(|| {
    let mut environment = minijinja::Environment::new();
    environment.set_syntax(
        minijinja::syntax::SyntaxConfig::builder()
            .keep_trailing_newline(true)
            .build()
            .expect("the default syntax"),
    );
    for (template, source) in [
        (Template::Review, REVIEW.as_str()),
        (Template::PrReview, PR_REVIEW.as_str()),
        (Template::PrComment, PR_COMMENT),
    ] {
        environment
            .add_template(template.name(), source)
            .expect("a valid template");
    }
    environment
});

pub fn render<M: serde::Serialize>(template: Template, model: &M) -> String {
    ENVIRONMENT
        .get_template(template.name())
        .and_then(|t| t.render(minijinja::context! { model => minijinja::value::Serde(model) }))
        .expect("a template that renders every model")
}

#[derive(serde::Serialize)]
pub struct ReportRule<'f> {
    pub flags: &'f str,
    pub findings: Vec<&'f Finding>,
}

pub fn by_rule<'f>(findings: &[&'f Finding]) -> Vec<ReportRule<'f>> {
    let mut sorted = findings.to_vec();
    sorted.sort_by(|a, b| {
        (a.finding.file.as_str(), a.finding.line_range.start())
            .cmp(&(b.finding.file.as_str(), b.finding.line_range.start()))
    });
    let mut rules: Vec<ReportRule<'f>> = Vec::new();
    for f in sorted {
        match rules.iter_mut().find(|rule| rule.flags == f.finding.flags) {
            Some(rule) => rule.findings.push(f),
            None => rules.push(ReportRule {
                flags: &f.finding.flags,
                findings: vec![f],
            }),
        }
    }
    rules
}

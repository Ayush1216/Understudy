"""Risk classification, run at record time and written into the artifact where a reviewer
sees it. Deliberately blunt: a navigation link named "Funds Transfer" reads as the
transaction. Over-classifying costs an operator one --max-risk flag; under-classifying posts
a transaction unreviewed."""

from __future__ import annotations

import re

from understudy.schema import Risk

_IRREVERSIBLE = re.compile(
    r"\b(submit|confirm|approve|post|transfer|delete|remove|disburse|authorize|sign off)\b", re.I
)
_SENSITIVE = re.compile(r"\b(save|update|change|edit|create|new|add|review)\b", re.I)


def classify_risk(*, action_type: str, control_name: str | None = "", is_form_submit: bool = False) -> Risk:
    name = control_name or ""
    if is_form_submit:
        return "irreversible"
    if action_type == "click":
        if _IRREVERSIBLE.search(name):
            return "irreversible"
        if _SENSITIVE.search(name):
            return "sensitive"
    return "safe"

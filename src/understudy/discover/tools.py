"""The tools the model may call. Every acting tool takes a `ref` from the latest observation
and a `why` that becomes the recorded step's description. Descriptions say exactly what the
loop does — a tool must not promise a screenshot it does not send."""

from __future__ import annotations

from typing import Any

NAME = r"^[a-z][a-z0-9_]*$"


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object", "properties": properties, "required": required, "additionalProperties": False,
            },
        },
    }


REF = {"type": "string", "description": "A ref (c1, c2, ...) from the LATEST observation."}
WHY = {"type": "string", "description": "One sentence: what this does towards the goal. Recorded as the step description."}
SENSITIVITY = {"type": "string", "enum": ["public", "pii", "secret"], "description": "Drives redaction and screenshot masking."}

TOOLS: list[dict[str, Any]] = [
    _tool(
        "observe",
        "Re-read the current screen and return a fresh observation with new refs. A screenshot is "
        "attached only on the first turn, when include_screenshot is true, and after a turn that made "
        "no visible progress; otherwise the observation is text.",
        {"include_screenshot": {"type": "boolean", "description": "Attach a screenshot to the observation.", "default": False}},
        [],
    ),
    _tool(
        "click",
        "Click the control with this ref (a button, link, checkbox or radio). Returns the observation after the click.",
        {"ref": REF, "why": WHY}, ["ref", "why"],
    ),
    _tool(
        "type_text",
        "Clear the textbox with this ref and type text into it. Type a supplied input value literally; "
        "type a credential as the placeholder {{secrets.NAME}} exactly (it is substituted when the action "
        "runs and never shown to you). Returns the observation after typing.",
        {"ref": REF, "text": {"type": "string"}, "why": WHY}, ["ref", "text", "why"],
    ),
    _tool(
        "select_option",
        "Select the option whose value or visible label is `value` in the combobox or listbox with this ref.",
        {"ref": REF, "value": {"type": "string"}, "why": WHY}, ["ref", "value", "why"],
    ),
    _tool(
        "press_key",
        "Press one keyboard key (Enter, Tab, Escape, F5, ...) on the control with this ref, or on the page when ref is omitted.",
        {"key": {"type": "string"}, "ref": REF, "why": WHY}, ["key", "why"],
    ),
    _tool(
        "navigate",
        "Load an absolute URL directly. Only URLs within the application's allowed origin are permitted.",
        {"url": {"type": "string"}, "why": WHY}, ["url", "why"],
    ),
    _tool(
        "extract",
        "Read the text of the cell or control with this ref as a named output of the automation: the cell "
        "next to a label, or a cell in a table with a header row. Returns the value seen now; the output is "
        "recorded with a durable description of where it was read, never by its value.",
        {
            "ref": REF,
            "output_name": {"type": "string", "pattern": NAME, "description": "snake_case output name."},
            "type": {"type": "string", "enum": ["string", "number", "boolean", "currency"]},
            "description": {"type": "string"},
            "transform": {"type": "string", "enum": ["none", "trim", "digits_only", "currency_to_number", "upper", "lower"]},
            "sensitivity": SENSITIVITY,
            "why": WHY,
        },
        ["ref", "output_name", "type", "description", "why"],
    ),
    _tool(
        "declare_input",
        "Declare a value a future caller supplies to this automation (a member number, an amount, ...). "
        "Use the same name as a supplied input where one exists, and the value you used as the example.",
        {
            "name": {"type": "string", "pattern": NAME},
            "type": {"type": "string", "enum": ["string", "number", "boolean", "enum"]},
            "description": {"type": "string"},
            "sensitivity": SENSITIVITY,
            "example": {"type": "string", "description": "The value used in this run."},
            "pattern": {"type": "string", "description": "Regex the value must match."},
            "enum_values": {"type": "array", "items": {"type": "string"}, "description": "Required when type is enum."},
        },
        ["name", "type", "description"],
    ),
    _tool(
        "declare_outcome",
        "Declare an alternative result of this flow that a caller must know about (e.g. MEMBER_NOT_FOUND, "
        "NOT_AUTHORIZED), detected by text that appears on the screen when it happens. Not for the happy path.",
        {
            "code": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]*$"},
            "description": {"type": "string"},
            "detect_text": {"type": "string", "description": "Text visible only when this outcome happens."},
        },
        ["code", "description", "detect_text"],
    ),
    _tool(
        "assert_checkpoint",
        "Assert that text is present on (or absent from) the current screen. Evaluated now and refused if it "
        "does not hold. Recorded as the postcondition of the previous action, so use text that holds for ANY "
        "record: headings, labels, column headers.",
        {"kind": {"type": "string", "enum": ["text_present", "text_absent"]}, "text": {"type": "string"}, "why": WHY},
        ["kind", "text", "why"],
    ),
    _tool(
        "finish",
        "Declare the goal accomplished. success_text must be visible on the current screen and must hold for any "
        "record; the call is refused otherwise. The run is then compiled into a reusable capability.",
        {"summary": {"type": "string", "description": "What the automation does, in one or two sentences."},
         "success_text": {"type": "string", "description": "Text on the final screen that proves success."}},
        ["summary", "success_text"],
    ),
    _tool(
        "request_human_help",
        "Hand the live session to a human operator. Use it when stuck, when an error blocks you, or when a step "
        "needs a human decision (an irreversible transaction, an approval). You resume with a fresh observation "
        "once the human hands control back.",
        {"reason": {"type": "string"}, "what_i_was_trying": {"type": "string"}}, ["reason", "what_i_was_trying"],
    ),
]

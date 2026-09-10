"""What perception produces: a distilled, model-facing view of the surface.

Refs (`c1`, `c2`, ...) are handles for ONE model turn. They are never persisted; the next
observation invalidates them. The durable identity of a control is its TargetDescriptor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import Role, StrictModel

# Where an inferred label came from. Recorded so the descriptor's rationale can say it.
LabelSource = Literal[
    "label_for", "label_wrap", "row_cell", "preceding_text", "geometric",
    "placeholder", "name_attr", "value", "aria", "none",
]


class Box(StrictModel):
    x: float
    y: float
    width: float
    height: float


class TablePosition(StrictModel):
    row_anchor: str      # text of the anchor (first) column in this control's row
    column: str          # header text of this control's column
    anchor_column: str   # header text of the anchor column


class PerceivedControl(StrictModel):
    ref: str
    frame_path: list[str]
    role: Role
    # Accessible name from the AX tree. Often "" on legacy markup — that is the whole problem.
    name: str = ""
    inferred_label: str = ""
    label_source: LabelSource = "none"
    label_confidence: float = 0.0
    # Only the attributes a strategy could use: name, id, placeholder, type, value, href, title, alt.
    attrs: dict[str, str] = Field(default_factory=dict)
    box: Box                # frame-local CSS px
    page_box: Box           # page-viewport CSS px (frame offsets applied)
    enabled: bool = True
    value: str | None = None
    table_position: TablePosition | None = None

    def display_name(self) -> str:
        return self.name or self.inferred_label or self.attrs.get("name") or ""


class PerceivedTable(StrictModel):
    ref: str
    frame_path: list[str]
    caption: str = ""
    columns: list[str]
    anchor_column: str
    rows: list[dict[str, str]]        # column header -> cell text
    cell_refs: list[dict[str, str]]   # column header -> ref, parallel to rows


class FramePerception(StrictModel):
    path: list[str]
    url: str
    title: str = ""
    # Headings, then table rows joined with " | ", then remaining visible lines. Truncated.
    text_digest: str = ""
    controls: list[PerceivedControl] = Field(default_factory=list)
    tables: list[PerceivedTable] = Field(default_factory=list)


class Observation(StrictModel):
    gen: int
    url: str
    title: str
    frames: list[FramePerception]
    document_status: int | None = None
    screenshot_b64: str | None = None
    warnings: list[str] = Field(default_factory=list)

    def all_controls(self) -> list[PerceivedControl]:
        return [c for f in self.frames for c in f.controls]

    def control(self, ref: str) -> PerceivedControl | None:
        return next((c for c in self.all_controls() if c.ref == ref), None)

    def all_text(self) -> str:
        return "\n".join(f.text_digest for f in self.frames)

    def signature(self) -> str:
        """Progress signature for the no-progress rule: URL + the set of control names."""
        names = sorted({f"{c.role}:{c.display_name()}" for c in self.all_controls()})
        return self.url + "|" + ",".join(names)

    def to_text(self, max_rows_per_table: int = 8) -> str:
        """The model-facing rendering. One line per control; tables as header + rows."""
        out: list[str] = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.document_status is not None:
            out.append(f"HTTP: {self.document_status}")
        for f in self.frames:
            fp = "/" + "/".join(f.path) if f.path else "/"
            out.append(f"\n[frame {fp}] {f.url}")
            for c in f.controls:
                bits = [f"  {c.role:<9}", f"ref={c.ref:<6}"]
                if c.name:
                    bits.append(f'"{c.name}"')
                if c.inferred_label and c.inferred_label != c.name:
                    bits.append(f'label="{c.inferred_label}" ({c.label_source})')
                if c.attrs.get("name"):
                    bits.append(f"name={c.attrs['name']}")
                if c.value:
                    bits.append(f'value="{c.value}"')
                if c.table_position:
                    tp = c.table_position
                    bits.append(f"cell[{tp.anchor_column}={tp.row_anchor}, col={tp.column}]")
                if not c.enabled:
                    bits.append("(disabled)")
                out.append(" ".join(bits))
            for t in f.tables:
                cap = f' "{t.caption}"' if t.caption else ""
                out.append(f"  table     ref={t.ref:<6}{cap} cols=[{' | '.join(t.columns)}] rows={len(t.rows)}")
                for row in t.rows[:max_rows_per_table]:
                    out.append("    " + "  ".join(f"{k}={v}" for k, v in row.items() if v))
                if len(t.rows) > max_rows_per_table:
                    out.append(f"    ... {len(t.rows) - max_rows_per_table} more rows")
            if f.text_digest:
                out.append("  text:")
                for line in f.text_digest.splitlines():
                    out.append("    " + line)
        if self.warnings:
            out.append("\nWARNINGS: " + "; ".join(self.warnings))
        return "\n".join(out)

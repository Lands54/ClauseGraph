"""Native properties of the four snapshot graph node types."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Contract, Index, Location, Name

Nonnegative = Annotated[int, Field(ge=0)]


class GraphNodeProperties(Contract):
    id: Name
    label: str


class DocumentProperties(GraphNodeProperties):
    kind: Literal["Document"] = "Document"


class ClauseProperties(GraphNodeProperties):
    kind: Literal["Clause"] = "Clause"
    local_clause_id: Name
    level: Nonnegative
    order: Nonnegative
    location_unit: Literal["docx_block", "markdown_line"] | None = None
    location_start: Index | None = None
    location_end: Index | None = None

    @model_validator(mode="after")
    def validate_location(self):
        location = (self.location_unit, self.location_start, self.location_end)
        if self.level == 0:
            if self.order != 0 or any(value is not None for value in location):
                raise ValueError("根条款顺序必须为 0，且无标题定位")
        else:
            if self.order == 0:
                raise ValueError("非根条款顺序必须为正整数")
            Location(unit=self.location_unit, start=self.location_start, end=self.location_end)
        return self


class TypeConceptProperties(GraphNodeProperties):
    kind: Literal["TypeConcept"] = "TypeConcept"
    uri: Name
    labels: list[str]
    document_types: list[str]


class ContentConceptProperties(GraphNodeProperties):
    kind: Literal["ContentConcept"] = "ContentConcept"


GRAPH_NODE_PROPERTIES = {
    "Document": DocumentProperties,
    "Clause": ClauseProperties,
    "TypeConcept": TypeConceptProperties,
    "ContentConcept": ContentConceptProperties,
}

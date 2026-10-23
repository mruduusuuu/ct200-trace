from pydantic import BaseModel, Field


class SelectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    node_ids: list[str] = Field(min_length=1, max_length=50, description="logical node ids (stable across versions)")
    version: int | None = Field(default=None, description="version to pin to; default = latest")
    include_descendants: bool = Field(default=True, description="pull in the whole subtree of each selected node")

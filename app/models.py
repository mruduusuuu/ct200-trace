from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocVersion(Base):
    __tablename__ = "doc_versions"
    __table_args__ = (UniqueConstraint("document_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    number: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(300))
    file_sha256: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    report: Mapped[dict] = mapped_column(JSON, default=dict)  # parser issues, stats, ingest summary
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LogicalNode(Base):
    """Stable identity of a section across versions. Public 'node id'."""
    __tablename__ = "logical_nodes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    created_in_version: Mapped[int] = mapped_column(Integer)


class NodeVersion(Base):
    """A logical node as it exists in one specific version (immutable once written)."""
    __tablename__ = "node_versions"
    __table_args__ = (
        UniqueConstraint("logical_node_id", "version_id"),
        Index("ix_nv_version_parent", "version_id", "parent_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    logical_node_id: Mapped[str] = mapped_column(ForeignKey("logical_nodes.id"), index=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("doc_versions.id"), index=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("node_versions.id"), nullable=True)
    section_number: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(Text)
    level: Mapped[int] = mapped_column(Integer)
    order_idx: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    change_status: Mapped[str] = mapped_column(String(20))  # initial | added | unchanged | modified
    matched_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    refs: Mapped[list] = mapped_column(JSON, default=list)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)

    version: Mapped[DocVersion] = relationship()
    parent: Mapped["NodeVersion | None"] = relationship(remote_side="NodeVersion.id")


class Selection(Base):
    __tablename__ = "selections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("doc_versions.id"))
    include_descendants: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[DocVersion] = relationship()


class SelectionItem(Base):
    """Version-pinned reference: points at a NodeVersion row, never at a bare node id."""
    __tablename__ = "selection_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    selection_id: Mapped[str] = mapped_column(ForeignKey("selections.id"), index=True)
    node_version_id: Mapped[int] = mapped_column(ForeignKey("node_versions.id"))
    logical_node_id: Mapped[str] = mapped_column(String(32), index=True)
    is_root: Mapped[bool] = mapped_column(Boolean, default=False)  # explicitly selected vs pulled in as descendant
    order_idx: Mapped[int] = mapped_column(Integer)
    node_version: Mapped[NodeVersion] = relationship()

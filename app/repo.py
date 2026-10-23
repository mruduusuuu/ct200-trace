"""Small query helpers over the tree tables."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DocVersion, LogicalNode, NodeVersion


def latest_version(s: Session, document_id: int) -> DocVersion | None:
    return s.scalars(select(DocVersion).where(DocVersion.document_id == document_id)
                     .order_by(DocVersion.number.desc())).first()


def get_version(s: Session, document_id: int, number: int) -> DocVersion | None:
    return s.scalars(select(DocVersion).where(DocVersion.document_id == document_id,
                                              DocVersion.number == number)).first()


def logical(s: Session, node_id: str) -> LogicalNode | None:
    return s.get(LogicalNode, node_id)


def node_in_version(s: Session, node_id: str, version_id: int) -> NodeVersion | None:
    return s.scalars(select(NodeVersion).where(NodeVersion.logical_node_id == node_id,
                                               NodeVersion.version_id == version_id)).first()


def node_history(s: Session, node_id: str) -> list[NodeVersion]:
    return list(s.scalars(select(NodeVersion).join(DocVersion).where(NodeVersion.logical_node_id == node_id)
                          .order_by(DocVersion.number)))


def children(s: Session, nv: NodeVersion) -> list[NodeVersion]:
    return list(s.scalars(select(NodeVersion).where(NodeVersion.parent_id == nv.id).order_by(NodeVersion.order_idx)))


def subtree(s: Session, nv: NodeVersion, include_self: bool = True) -> list[NodeVersion]:
    """Pre-order (document order) list of nv and all descendants within the same version."""
    out = [nv] if include_self else []
    for c in children(s, nv):
        out.extend(subtree(s, c, True))
    return out


def breadcrumbs(nv: NodeVersion) -> list[NodeVersion]:
    chain, cur = [], nv
    while cur is not None:
        chain.append(cur)
        cur = cur.parent
    return list(reversed(chain))


def version_nodes(s: Session, version_id: int) -> list[NodeVersion]:
    return list(s.scalars(select(NodeVersion).where(NodeVersion.version_id == version_id).order_by(NodeVersion.order_idx)))

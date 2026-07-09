"""Dashboard relationships routes"""

from __future__ import annotations

from fastapi import HTTPException

from mira.dashboard.api import (
    CustomEdgeModel,
    CustomEdgeRequest,
    OverrideModel,
    OverrideRequest,
    RelatedRepoModel,
    RelationshipsResponse,
    RepoEdgeModel,
    RepoGroupModel,
    _open_relationships,
    router,
)


@router.get("/api/relationships", response_model=RelationshipsResponse)
def get_relationships() -> RelationshipsResponse:
    """Get all cross-repo edges and groups."""
    with _open_relationships() as rs:
        edges = rs.resolve_edges()
        groups = rs.group_repos(rs.repos)

        return RelationshipsResponse(
            edges=[
                RepoEdgeModel(
                    source_repo=e.source_repo,
                    target_repo=e.target_repo,
                    kind=e.kind,
                    ref_count=len(e.refs),
                )
                for e in edges
            ],
            groups=[
                RepoGroupModel(
                    name=g.name,
                    repos=g.repos,
                    confidence=g.confidence,
                    evidence=g.evidence,
                )
                for g in groups
            ],
        )


@router.get("/api/relationships/{owner}/{repo}", response_model=list[RelatedRepoModel])
def get_related_repos(owner: str, repo: str) -> list[RelatedRepoModel]:
    """Get repos related to a specific repo."""
    with _open_relationships() as rs:
        full_name = f"{owner}/{repo}"
        if full_name not in rs.repos:
            raise HTTPException(status_code=404, detail=f"Repo {full_name} not found in index")

        related = rs.get_related_repos(owner, repo)

        return [
            RelatedRepoModel(
                repo=repo_name,
                relationship_type=rel_type,
                edge_count=len(edges),
            )
            for repo_name, rel_type, edges in related
        ]


@router.get("/api/relationships/overrides", response_model=list[OverrideModel])
def list_overrides() -> list[OverrideModel]:
    with _open_relationships() as rs:
        return [
            OverrideModel(
                source_repo=o.source_repo,
                target_repo=o.target_repo,
                status=o.status,
                created_at=o.created_at,
            )
            for o in rs.list_overrides()
        ]


@router.post("/api/relationships/overrides", response_model=OverrideModel)
def set_override(body: OverrideRequest) -> OverrideModel:
    if body.status not in ("confirmed", "denied"):
        raise HTTPException(status_code=400, detail="Status must be 'confirmed' or 'denied'")
    with _open_relationships() as rs:
        o = rs.set_override(body.source_repo, body.target_repo, body.status)
        return OverrideModel(
            source_repo=o.source_repo,
            target_repo=o.target_repo,
            status=o.status,
            created_at=o.created_at,
        )


@router.delete("/api/relationships/overrides")
def delete_override(source_repo: str, target_repo: str) -> dict:
    with _open_relationships() as rs:
        rs.delete_override(source_repo, target_repo)
        return {"ok": True}


@router.get("/api/relationships/custom", response_model=list[CustomEdgeModel])
def list_custom_edges() -> list[CustomEdgeModel]:
    with _open_relationships() as rs:
        return [
            CustomEdgeModel(
                id=e.id,
                source_repo=e.source_repo,
                target_repo=e.target_repo,
                reason=e.reason,
                created_at=e.created_at,
            )
            for e in rs.list_custom_edges()
        ]


@router.post("/api/relationships/custom", response_model=CustomEdgeModel)
def add_custom_edge(body: CustomEdgeRequest) -> CustomEdgeModel:
    with _open_relationships() as rs:
        e = rs.add_custom_edge(body.source_repo, body.target_repo, body.reason)
        return CustomEdgeModel(
            id=e.id,
            source_repo=e.source_repo,
            target_repo=e.target_repo,
            reason=e.reason,
            created_at=e.created_at,
        )


@router.delete("/api/relationships/custom/{edge_id}")
def delete_custom_edge(edge_id: int) -> dict:
    with _open_relationships() as rs:
        rs.delete_custom_edge(edge_id)
        return {"ok": True}

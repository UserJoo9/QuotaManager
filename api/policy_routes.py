"""Policy API routes for Quota Manager.

Provides REST endpoints for the Unified Policy Engine:
- CRUD operations on policies
- Policy evaluation and simulation
- Capability detection
- Application catalog
- Enforcement registry status
"""
from __future__ import annotations

import json
import logging
import datetime as _dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from dataclasses import asdict

from quota.db import Database, save_policy, list_policies as db_list_policies, get_policy as db_get_policy, delete_policy as db_delete_policy, audit_policy_log, list_audit_log, init_rbac_tables, rbac_user_has_permission
from quota.policy_engine import (
    Policy, PolicyEngine, PolicyScope, PolicyTarget, PolicyAction,
    EnforcementMethod, PolicyCondition, PolicySchedule,
)
from quota.app_catalog import to_dict as catalog_to_dict
from quota.capabilities import detect_capabilities, get_capability_summary
from quota.policy_adapters import EnforcementRegistry

log = logging.getLogger("quota.policy_api")

router = APIRouter(prefix="/api/policies", tags=["policies"])

# Global engine instance (initialized at app startup)
_policy_engine: PolicyEngine | None = None
_enforcement_registry: EnforcementRegistry | None = None
_db: Database | None = None
_current_user_id: str | None = None  # set by middleware before each request


def init_policy_engine(db: Database, engine: PolicyEngine, registry: EnforcementRegistry, current_user_id: str | None = None) -> None:
    global _policy_engine, _enforcement_registry, _db, _current_user_id
    _db = db
    _policy_engine = engine
    _enforcement_registry = registry
    _current_user_id = current_user_id


def set_current_user(user_id: str | None) -> None:
    """Set the current user for RBAC checks (called by middleware)."""
    global _current_user_id
    _current_user_id = user_id


def get_current_user() -> str | None:
    return _current_user_id


def require_permission(permission: str):
    """Decorator factory: require a specific RBAC permission."""
    def decorator(func):
        import functools
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if _db is None:
                raise HTTPException(503, "Database not initialized")
            user_id = _current_user_id or "admin"
            if not await rbac_user_has_permission(_db, user_id, permission):
                raise HTTPException(403, f"Permission required: {permission}")
            return await func(*args, **kwargs)
        return wrapper
    return decorator


def get_engine() -> PolicyEngine:
    if _policy_engine is None:
        raise HTTPException(503, "Policy engine not initialized")
    return _policy_engine


def get_registry() -> EnforcementRegistry:
    if _enforcement_registry is None:
        raise HTTPException(503, "Enforcement registry not initialized")
    return _enforcement_registry


def get_db() -> Database:
    if _db is None:
        raise HTTPException(503, "Database not initialized")
    return _db


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #

class PolicyCreate(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    scope_type: str = "global"
    scope_targets: list[str] = Field(default_factory=list)
    target_type: str = "app"
    target_values: list[str] = Field(default_factory=list)
    action: str = "block"
    action_params: dict[str, Any] = Field(default_factory=dict)
    conditions: list[dict[str, Any]] = Field(default_factory=list)
    schedule: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    exceptions: list[str] = Field(default_factory=list)
    expires_at: str = ""
    enforcement_method: str = "dns"
    log_enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class PolicyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    scope_type: str | None = None
    scope_targets: list[str] | None = None
    target_type: str | None = None
    target_values: list[str] | None = None
    action: str | None = None
    action_params: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] | None = None
    schedule: dict[str, Any] | None = None
    priority: int | None = None
    exceptions: list[str] | None = None
    expires_at: str | None = None
    enforcement_method: str | None = None
    log_enabled: bool | None = None
    tags: list[str] | None = None


class EvaluateRequest(BaseModel):
    target: str
    context: dict[str, Any] = Field(default_factory=dict)


class SimulateRequest(BaseModel):
    target: str
    context: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _policy_from_create(body: PolicyCreate, policy_id: str) -> Policy:
    conds = [PolicyCondition(**c) for c in body.conditions]
    sched = PolicySchedule(**body.schedule) if body.schedule else PolicySchedule()
    return Policy(
        id=policy_id,
        name=body.name,
        description=body.description,
        enabled=body.enabled,
        scope_type=PolicyScope(body.scope_type),
        scope_targets=body.scope_targets,
        target_type=PolicyTarget(body.target_type),
        target_values=body.target_values,
        action=PolicyAction(body.action),
        action_params=body.action_params,
        conditions=conds,
        schedule=sched,
        priority=body.priority,
        exceptions=body.exceptions,
        expires_at=body.expires_at,
        enforcement_method=EnforcementMethod(body.enforcement_method),
        log_enabled=body.log_enabled,
        tags=body.tags,
    )


def _policy_to_dict(p: Policy) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "description": p.description,
        "enabled": p.enabled,
        "scope_type": p.scope_type.value,
        "scope_targets": p.scope_targets,
        "target_type": p.target_type.value,
        "target_values": p.target_values,
        "action": p.action.value,
        "action_params": p.action_params,
        "conditions": [asdict(c) for c in p.conditions],
        "schedule": {
            "enabled": p.schedule.enabled,
            "days": p.schedule.days,
            "time_ranges": p.schedule.time_ranges,
            "timezone": p.schedule.timezone,
            "start_date": p.schedule.start_date,
            "end_date": p.schedule.end_date,
        },
        "priority": p.priority,
        "exceptions": p.exceptions,
        "expires_at": p.expires_at,
        "enforcement_method": p.enforcement_method.value,
        "log_enabled": p.log_enabled,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "created_by": p.created_by,
        "tags": p.tags,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("")
async def list_policies(enabled_only: bool = False):
    """List all policies."""
    db = get_db()
    rows = await db_list_policies(db, enabled_only=enabled_only)
    return {"policies": rows, "count": len(rows)}


@router.get("/capabilities")
async def get_capabilities():
    """Get capability matrix for the current environment."""
    return detect_capabilities()


@router.get("/conflicts")
async def get_policy_conflicts():
    """Detect conflicting policies — same target with different actions and overlapping scopes."""
    engine = get_engine()
    policies = engine.list_policies(enabled_only=True)
    conflicts = []
    
    # Group by target
    by_target: dict[str, list] = {}
    for p in policies:
        for tv in p.target_values:
            by_target.setdefault(tv, []).append(p)
    
    for target, ps in by_target.items():
        if len(ps) < 2:
            continue
        # Check for block vs allow conflicts
        blocks = [p for p in ps if p.action == PolicyAction.BLOCK]
        allows = [p for p in ps if p.action == PolicyAction.ALLOW]
        
        if blocks and allows:
            # Find the highest priority block and allow
            max_block = max(blocks, key=lambda p: p.priority)
            max_allow = max(allows, key=lambda p: p.priority)
            
            conflicts.append({
                "target": target,
                "conflict_type": "block_vs_allow",
                "block_policy": {"id": max_block.id, "name": max_block.name, "priority": max_block.priority, "scope": max_block.scope_type.value},
                "allow_policy": {"id": max_allow.id, "name": max_allow.name, "priority": max_allow.priority, "scope": max_allow.scope_type.value},
                "resolution": "block" if max_block.priority >= max_allow.priority else "allow",
                "explanation": f"Conflict: '{max_block.name}' blocks '{target}' while '{max_allow.name}' allows it. Higher priority wins.",
            })
    
    return {"conflicts": conflicts, "count": len(conflicts)}


@router.get("/audit-log")
async def get_audit_log(limit: int = 100):
    """Get policy audit log entries."""
    db = get_db()
    rows = await list_audit_log(db, limit=limit)
    return {"audit_log": rows, "count": len(rows)}


@router.get("/{policy_id}")
async def get_policy(policy_id: str):
    """Get a single policy by ID."""
    db = get_db()
    p = await db_get_policy(db, policy_id)
    if not p:
        raise HTTPException(404, "Policy not found")
    return p


@router.post("")
@require_permission("policy:create")
async def create_policy(body: PolicyCreate):
    """Create a new policy."""
    import uuid
    import datetime as _dt
    db = get_db()
    engine = get_engine()
    policy_id = f"pol_{uuid.uuid4().hex[:12]}"
    now = _dt.datetime.now().astimezone().isoformat()
    policy = _policy_from_create(body, policy_id)
    policy.created_at = now
    policy.updated_at = now
    await save_policy(db, _policy_to_dict(policy))
    await audit_policy_log(db, "create", policy_id, policy.name, actor="admin", details={"scope": policy.scope_type.value, "target": policy.target_type.value, "action": policy.action.value})
    engine.add_policy(policy)
    return {"id": policy_id, "policy": _policy_to_dict(policy)}


@router.put("/{policy_id}")
@require_permission("policy:update")
async def update_policy(policy_id: str, body: PolicyUpdate):
    """Update an existing policy."""
    db = get_db()
    engine = get_engine()
    existing = await db_get_policy(db, policy_id)
    if not existing:
        raise HTTPException(404, "Policy not found")
    # Apply updates
    if body.name is not None:
        existing["name"] = body.name
    if body.description is not None:
        existing["description"] = body.description
    if body.enabled is not None:
        existing["enabled"] = body.enabled
    if body.scope_type is not None:
        existing["scope_type"] = body.scope_type
    if body.scope_targets is not None:
        existing["scope_targets"] = body.scope_targets
    if body.target_type is not None:
        existing["target_type"] = body.target_type
    if body.target_values is not None:
        existing["target_values"] = body.target_values
    if body.action is not None:
        existing["action"] = body.action
    if body.action_params is not None:
        existing["action_params"] = body.action_params
    if body.conditions is not None:
        existing["conditions"] = body.conditions
    if body.schedule is not None:
        existing["schedule"] = body.schedule
    if body.priority is not None:
        existing["priority"] = body.priority
    if body.exceptions is not None:
        existing["exceptions"] = body.exceptions
    if body.expires_at is not None:
        existing["expires_at"] = body.expires_at
    if body.enforcement_method is not None:
        existing["enforcement_method"] = body.enforcement_method
    if body.log_enabled is not None:
        existing["log_enabled"] = body.log_enabled
    if body.tags is not None:
        existing["tags"] = body.tags
    existing["updated_at"] = _dt.datetime.now().astimezone().isoformat()
    await save_policy(db, existing)
    await audit_policy_log(db, "update", policy_id, existing["name"], actor="admin", details={"changes": list(body.model_dump(exclude_unset=True).keys())})
    # Rebuild engine from DB
    engine._policies.clear()
    for p in await db_list_policies(db):
        conds = [PolicyCondition(**c) for c in p.get("conditions", [])]
        sched = PolicySchedule(**p.get("schedule", {})) if p.get("schedule") else PolicySchedule()
        engine.add_policy(Policy(
            id=p["id"], name=p["name"], description=p.get("description", ""),
            enabled=p.get("enabled", True),
            scope_type=PolicyScope(p.get("scope_type", "global")),
            scope_targets=p.get("scope_targets", []),
            target_type=PolicyTarget(p.get("target_type", "app")),
            target_values=p.get("target_values", []),
            action=PolicyAction(p.get("action", "block")),
            action_params=p.get("action_params", {}),
            conditions=conds,
            schedule=sched,
            priority=p.get("priority", 0),
            exceptions=p.get("exceptions", []),
            expires_at=p.get("expires_at", ""),
            enforcement_method=EnforcementMethod(p.get("enforcement_method", "dns")),
            log_enabled=p.get("log_enabled", True),
            created_at=p.get("created_at", ""),
            updated_at=p.get("updated_at", ""),
            created_by=p.get("created_by", "admin"),
            tags=p.get("tags", []),
        ))
    return existing


@router.delete("/{policy_id}")
@require_permission("policy:delete")
async def delete_policy(policy_id: str):
    """Delete a policy."""
    db = get_db()
    engine = get_engine()
    existing = await db_get_policy(db, policy_id)
    if not existing:
        raise HTTPException(404, "Policy not found")
    if not await db_delete_policy(db, policy_id):
        raise HTTPException(404, "Policy not found")
    await audit_policy_log(db, "delete", policy_id, existing["name"], actor="admin")
    engine.remove_policy(policy_id)
    return {"deleted": policy_id}


@router.post("/evaluate")
async def evaluate_policy(body: EvaluateRequest):
    """Evaluate all policies against a target + context. Returns all matched + resolved."""
    engine = get_engine()
    decisions = engine.evaluate(body.target, body.context)
    resolved = engine.resolve(body.target, body.context)
    return {
        "target": body.target,
        "context": body.context,
        "matched": [
            {"policy_id": d.policy_id, "action": d.action.value, "reason": d.reason, "priority": d.priority}
            for d in decisions
        ],
        "resolved": {
            "action": resolved.action.value,
            "reason": resolved.reason,
            "denied_by": resolved.denied_by,
            "allowed_by": resolved.allowed_by,
            "explain": resolved.explain,
        },
    }


@router.post("/simulate")
async def simulate_policy(body: SimulateRequest):
    """Simulate policy evaluation (dry run, no enforcement)."""
    engine = get_engine()
    return engine.simulate(body.target, body.context)


@router.get("/capabilities/summary")
async def get_capability_summary_text():
    """Get human-readable capability summary."""
    return {"summary": get_capability_summary()}


@router.get("/catalog")
async def get_app_catalog():
    """Get the full application catalog."""
    return catalog_to_dict()


@router.get("/catalog/{app_id}")
async def get_app(app_id: str):
    """Get a single app definition."""
    from quota.app_catalog import get_app as _get_app, get_app_features
    app = _get_app(app_id)
    if not app:
        raise HTTPException(404, "App not found")
    import dataclasses
    return {
        "app": dataclasses.asdict(app),
        "features": [dataclasses.asdict(f) for f in get_app_features(app_id)],
    }


@router.get("/enforcement/status")
async def get_enforcement_status():
    """Get enforcement adapter status."""
    registry = get_registry()
    return {
        "available_methods": registry.available_methods(),
        "capabilities": registry.capability_matrix(),
    }


# ---- RBAC endpoints ----

@router.get("/rbac/roles")
@require_permission("rbac:manage")
async def list_roles():
    """List all admin roles."""
    db = get_db()
    rows = (await db.conn.execute("SELECT role_id, role_name, description, created_at FROM admin_roles")).fetchall()
    return {"roles": [{"role_id": r[0], "role_name": r[1], "description": r[2], "created_at": r[3]} for r in rows]}


@router.post("/rbac/roles")
@require_permission("rbac:manage")
async def create_role(body: dict):
    """Create a new admin role."""
    db = get_db()
    role_id = body.get("role_id")
    role_name = body.get("role_name")
    description = body.get("description", "")
    if not role_id or not role_name:
        raise HTTPException(400, "role_id and role_name required")
    ok = await rbac_create_role(db, role_id, role_name, description)
    if not ok:
        raise HTTPException(400, "Role already exists")
    return {"role_id": role_id, "role_name": role_name}


@router.post("/rbac/roles/{role_id}/permissions/{permission}")
@require_permission("rbac:manage")
async def grant_permission(role_id: str, permission: str):
    """Grant a permission to a role."""
    db = get_db()
    ok = await rbac_grant_permission(db, role_id, permission)
    if not ok:
        raise HTTPException(400, "Failed to grant permission")
    return {"role_id": role_id, "permission": permission, "granted": True}


@router.post("/rbac/users/{user_id}/roles/{role_id}")
@require_permission("rbac:manage")
async def assign_role(user_id: str, role_id: str):
    """Assign a role to a user."""
    db = get_db()
    ok = await rbac_assign_role(db, user_id, role_id, assigned_by=_current_user_id or "admin")
    if not ok:
        raise HTTPException(400, "Failed to assign role")
    return {"user_id": user_id, "role_id": role_id, "assigned": True}


@router.get("/rbac/users/{user_id}/permissions")
@require_permission("rbac:manage")
async def user_permissions(user_id: str):
    """List all permissions for a user."""
    db = get_db()
    rows = (await db.conn.execute("""
        SELECT DISTINCT rp.permission FROM role_permissions rp
        JOIN admin_user_roles aur ON aur.role_id = rp.role_id
        WHERE aur.user_id = ?
    """, (user_id,))).fetchall()
    return {"user_id": user_id, "permissions": [r[0] for r in rows]}
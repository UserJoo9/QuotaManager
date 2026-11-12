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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from dataclasses import asdict

from quota.policy_engine import (
    Policy, PolicyEngine, PolicyScope, PolicyTarget, PolicyAction,
    EnforcementMethod, PolicyCondition, PolicySchedule,
)
from quota.app_catalog import APP_CATALOG, CONTENT_CATEGORIES, get_capability_matrix, to_dict as catalog_to_dict
from quota.capabilities import detect_capabilities, get_capability_summary
from quota.policy_adapters import EnforcementRegistry, DnsEnforcementAdapter, FirewallEnforcementAdapter, TrafficControlAdapter, QuotaEnforcementAdapter
from quota.policy_scheduler import PolicyScheduler

log = logging.getLogger("quota.policy_api")

router = APIRouter(prefix="/api/policies", tags=["policies"])

# Global engine instance (initialized at app startup)
_policy_engine: PolicyEngine | None = None
_enforcement_registry: EnforcementRegistry | None = None
_policy_scheduler: PolicyScheduler | None = None


def init_policy_engine(engine: PolicyEngine, registry: EnforcementRegistry, scheduler: PolicyScheduler) -> None:
    global _policy_engine, _enforcement_registry, _policy_scheduler
    _policy_engine = engine
    _enforcement_registry = registry
    _policy_scheduler = scheduler


def get_engine() -> PolicyEngine:
    if _policy_engine is None:
        raise HTTPException(503, "Policy engine not initialized")
    return _policy_engine


def get_registry() -> EnforcementRegistry:
    if _enforcement_registry is None:
        raise HTTPException(503, "Enforcement registry not initialized")
    return _enforcement_registry


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


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("")
async def list_policies(enabled_only: bool = False):
    """List all policies."""
    engine = get_engine()
    policies = engine.list_policies(enabled_only=enabled_only)
    return {"policies": [asdict(p) for p in policies], "count": len(policies)}


@router.get("/{policy_id}")
async def get_policy(policy_id: str):
    """Get a single policy by ID."""
    engine = get_engine()
    p = engine.get_policy(policy_id)
    if not p:
        raise HTTPException(404, "Policy not found")
    return asdict(p)


@router.post("")
async def create_policy(body: PolicyCreate):
    """Create a new policy."""
    import uuid
    engine = get_engine()
    policy_id = f"pol_{uuid.uuid4().hex[:12]}"
    policy = _policy_from_create(body, policy_id)
    engine.add_policy(policy)
    return {"id": policy_id, "policy": asdict(policy)}


@router.put("/{policy_id}")
async def update_policy(policy_id: str, body: PolicyUpdate):
    """Update an existing policy."""
    engine = get_engine()
    existing = engine.get_policy(policy_id)
    if not existing:
        raise HTTPException(404, "Policy not found")
    # Apply updates
    if body.name is not None:
        existing.name = body.name
    if body.description is not None:
        existing.description = body.description
    if body.enabled is not None:
        existing.enabled = body.enabled
    if body.scope_type is not None:
        existing.scope_type = PolicyScope(body.scope_type)
    if body.scope_targets is not None:
        existing.scope_targets = body.scope_targets
    if body.target_type is not None:
        existing.target_type = PolicyTarget(body.target_type)
    if body.target_values is not None:
        existing.target_values = body.target_values
    if body.action is not None:
        existing.action = PolicyAction(body.action)
    if body.action_params is not None:
        existing.action_params = body.action_params
    if body.conditions is not None:
        existing.conditions = [PolicyCondition(**c) for c in body.conditions]
    if body.schedule is not None:
        existing.schedule = PolicySchedule(**body.schedule) if body.schedule else PolicySchedule()
    if body.priority is not None:
        existing.priority = body.priority
    if body.exceptions is not None:
        existing.exceptions = body.exceptions
    if body.expires_at is not None:
        existing.expires_at = body.expires_at
    if body.enforcement_method is not None:
        existing.enforcement_method = EnforcementMethod(body.enforcement_method)
    if body.log_enabled is not None:
        existing.log_enabled = body.log_enabled
    if body.tags is not None:
        existing.tags = body.tags
    engine.update_policy(existing)
    return asdict(existing)


@router.delete("/{policy_id}")
async def delete_policy(policy_id: str):
    """Delete a policy."""
    engine = get_engine()
    if not engine.remove_policy(policy_id):
        raise HTTPException(404, "Policy not found")
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


@router.get("/capabilities")
async def get_capabilities():
    """Get capability matrix for the current environment."""
    return detect_capabilities()


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
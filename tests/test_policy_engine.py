"""Tests for the Unified Policy Engine."""
from __future__ import annotations

import pytest
import asyncio

from quota.policy_engine import (
    Policy, PolicyEngine, PolicyScope, PolicyTarget, PolicyAction,
    EnforcementMethod, PolicyCondition, PolicySchedule, ResolvedDecision,
)
from quota.db import save_policy, list_policies as db_list_policies, get_policy as db_get_policy, delete_policy as db_delete_policy


def test_policy_basic_block():
    engine = PolicyEngine()
    p = Policy(
        id="p1", name="Block TikTok",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
        priority=10,
    )
    engine.add_policy(p)
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.BLOCK
    assert decision.denied_by == "p1"


def test_policy_allow_override():
    engine = PolicyEngine()
    # Global block
    engine.add_policy(Policy(
        id="p1", name="Block TikTok globally",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
        priority=5,
    ))
    # User exception (higher priority)
    engine.add_policy(Policy(
        id="p2", name="Allow TikTok for user 1",
        scope_type=PolicyScope.USER,
        scope_targets=["1"],
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.ALLOW,
        priority=10,
    ))
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.ALLOW
    assert decision.allowed_by == "p2"


def test_policy_deny_wins_same_priority():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Allow",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.ALLOW,
        priority=5,
    ))
    engine.add_policy(Policy(
        id="p2", name="Block",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
        priority=5,
    ))
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.BLOCK


def test_policy_no_match_allows():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block YouTube",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["youtube"],
        action=PolicyAction.BLOCK,
    ))
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.ALLOW
    assert decision.reason == "no_matching_policy"


def test_policy_scope_mismatch():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block for user 2 only",
        scope_type=PolicyScope.USER,
        scope_targets=["2"],
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
    ))
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.ALLOW


def test_policy_exception():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block TikTok",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
        exceptions=["special_device"],
    ))
    decision = engine.resolve("tiktok", {"device_id": "special_device"})
    assert decision.action == PolicyAction.ALLOW


def test_policy_disabled():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block TikTok",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
        enabled=False,
    ))
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.ALLOW


def test_policy_expired():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block TikTok",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
        expires_at="2020-01-01T00:00:00+00:00",
    ))
    decision = engine.resolve("tiktok", {"user_id": 1})
    assert decision.action == PolicyAction.ALLOW


def test_policy_domain_wildcard():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block ads",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.DOMAIN,
        target_values=["*.doubleclick.net"],
        action=PolicyAction.BLOCK,
    ))
    decision = engine.resolve("ads.doubleclick.net", {"user_id": 1})
    assert decision.action == PolicyAction.BLOCK
    decision2 = engine.resolve("example.com", {"user_id": 1})
    assert decision2.action == PolicyAction.ALLOW


def test_policy_simulation():
    engine = PolicyEngine()
    engine.add_policy(Policy(
        id="p1", name="Block TikTok",
        scope_type=PolicyScope.GLOBAL,
        target_type=PolicyTarget.APP,
        target_values=["tiktok"],
        action=PolicyAction.BLOCK,
    ))
    result = engine.simulate("tiktok", {"user_id": 1})
    assert result["resolved_action"] == "block"
    assert "note" in result
    assert "Simulation only" in result["note"]


def test_policy_condition_time_range():
    cond = PolicyCondition(field="hour", operator="between", value=[9, 17])
    assert cond.evaluate({"hour": 12}) is True
    assert cond.evaluate({"hour": 20}) is False


def test_policy_condition_in():
    cond = PolicyCondition(field="user_id", operator="in", value=[1, 2, 3])
    assert cond.evaluate({"user_id": 2}) is True
    assert cond.evaluate({"user_id": 5}) is False


def test_policy_schedule():
    sched = PolicySchedule(enabled=True, days=[0, 1, 2, 3, 4], time_ranges=[[8, 0, 15, 0]])
    # Just check it doesn't crash
    result = sched.is_active_now()
    assert isinstance(result, bool)


def test_policy_engine_serialization():
    engine = PolicyEngine()
    engine.add_policy(Policy(id="p1", name="Test", target_values=["app1"]))
    data = engine.to_dict()
    restored = PolicyEngine.from_dict(data)
    assert len(restored.list_policies()) == 1
    assert restored.get_policy("p1").name == "Test"


def test_policy_remove():
    engine = PolicyEngine()
    engine.add_policy(Policy(id="p1", name="Test"))
    assert engine.remove_policy("p1") is True
    assert engine.remove_policy("p1") is False
    assert len(engine.list_policies()) == 0


def test_policy_list_sorted_by_priority():
    engine = PolicyEngine()
    engine.add_policy(Policy(id="low", name="Low", priority=1))
    engine.add_policy(Policy(id="high", name="High", priority=10))
    engine.add_policy(Policy(id="mid", name="Mid", priority=5))
    policies = engine.list_policies()
    assert policies[0].id == "high"
    assert policies[1].id == "mid"
    assert policies[2].id == "low"


# ---- API-level CRUD tests ----

def test_api_policy_crud():
    """Test full CRUD cycle for policies via database."""
    import asyncio
    from quota.db import Database

    async def run():
        db = Database(":memory:")
        await db.connect()

        # Test save + list
        pid = await save_policy(db, {
            "id": "pol_test1", "name": "Test Policy", "description": "A test",
            "enabled": True, "scope_type": "global", "scope_targets": [],
            "target_type": "app", "target_values": ["tiktok"],
            "action": "block", "action_params": {},
            "conditions": [], "schedule": {}, "priority": 10,
            "exceptions": [], "expires_at": "",
            "enforcement_method": "dns", "log_enabled": True,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
            "created_by": "admin", "tags": [],
        })
        assert pid == "pol_test1"

        policies = await db_list_policies(db)
        assert len(policies) == 1
        assert policies[0]["name"] == "Test Policy"

        # Test get
        p = await db_get_policy(db, "pol_test1")
        assert p is not None
        assert p["name"] == "Test Policy"
        assert p["target_values"] == ["tiktok"]

        # Test delete
        assert await db_delete_policy(db, "pol_test1") is True
        assert await db_get_policy(db, "pol_test1") is None
        assert await db_delete_policy(db, "pol_test1") is False

        policies = await db_list_policies(db)
        assert len(policies) == 0

    asyncio.get_event_loop().run_until_complete(run())
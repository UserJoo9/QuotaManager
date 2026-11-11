# .refactor/STATE.md
# Total Project Refactor — Orchestrator State
# Branch: refactor/total (created from main @ pre-refactor tag)
# Language: Egyptian Arabic for reporting

## Current Phase
Phase 3 in progress — Wave A complete (U1, U2, U3 done). Moving to Wave B (U4, U5, U6).

## Wave
Wave A — Backend foundations U1/U2/U3: DONE (3 commits)
Wave B — Backend foundations U4/U5/U6: pending

## Per-Unit Status
- U1 (config+db+service): DONE — commit ab33a35, 3 pure domain helpers extracted, 137 tests pass
- U2 (nftables+firewall): DONE — commit 7078c95, _sync_set + _render_bans_drop extracted, 16/16 tests pass
- U3 (shaping+vpnshare): DONE — commit 30533eb, shaping.py cleaned, vpnshare.py verified clean, 107 tests pass
- U4 (history+analytics+DNS+ARP): pending
- U5 (IPv6+network+VPN/TUN+wifi/latency): pending
- U6 (storage+security+__init__): pending
- U7-U15: pending

## Last Green Commit
HEAD = refactor/total (30533eb). test_new_ui.py 16/16 pass, 0 JS syntax errors.

## Open Blockers
None

## Notes
- Working tree has unrelated pre-existing modifications to quota/firewall.py and quota/nftables.py (flagged by U2 sub-agent, outside U1/U3 allowlists, left uncommitted). Decide whether to commit or stash these in a later wave.
- Follow-ups logged: add unit tests for new pure helpers; optionally fold _sync_known_ips/_sync_pending_guests into _sync_set; resolve dormant _lan_counter_name IPv6-escaping; split TcShaper._tree_cmds helpers.
- Constitution: .refactor/specs/memory/constitution.md. Plan: .refactor/01-plan.md (17 units, 7 waves).
- Master prompt constraints: one commit per task, conventional commits, Egyptian Arabic reporting, never touch main, no force-push.

<!--
Sync Impact Report
Version change: none → 1.0.0 (initial draft; no previous constitution)
Modified principles: none
Added principles: Linux-owned datapath; clear module ownership; explicit quota semantics;
  IPv4/IPv6 parity; safe and recoverable host changes; responsive async control plane;
  durable state and secret handling; regression tests at system boundaries;
  consistent operator experience.
Added sections: Core Principles, Engineering Standards, Governance.
Removed sections: none.
Templates: ⚠ .specify/templates/plan-template.md,
  .specify/templates/spec-template.md, .specify/templates/tasks-template.md, and
  .specify/templates/commands/*.md are absent; no template synchronization was possible.
Runtime guidance: ✅ AGENTS.md, CLAUDE.md, and .agents/rules/documentation.md now
  point to this constitution.
Deferred: TODO(RATIFICATION_DATE) — maintainer ratification and original adoption date.
-->

# QuotaManager Constitution

**Version:** 1.0.0
**Ratified:** TODO(RATIFICATION_DATE): pending maintainer approval
**Last Amended:** 2026-11-10
**Status:** Draft

## Core Principles

### I. Linux Owns the Datapath

Linux kernel facilities own packet forwarding, filtering, accounting, and shaping.
Python configures and observes that state; it MUST NOT become a per-packet forwarding
path or maintain a competing source of network truth. Kernel-facing changes belong in
the owning nftables, traffic-control, routing, or DNS adapter.

This boundary keeps traffic handling predictable and lets operators inspect the
effective network state with standard Linux tools.

### II. Clear Module Ownership

Each behavior MUST have one clear owner. Quota policy belongs in the quota service,
persistence belongs in the database layer, request validation and HTTP orchestration
belong in the API, host commands belong in their subsystem adapters, and process
lifecycle and dependency wiring belong in Gateway.

Modules MUST depend on explicit collaborators rather than reach across layers for
unrelated responsibilities. Host-facing command runners and paths MUST be injectable
so tests can exercise behavior without changing the host.

### III. Explicit Quota Semantics

Quota accounting MUST be consistent across every device assigned to a user. Local
LAN traffic MUST remain outside metered Internet usage, and quota enforcement MUST
block Internet access without unintentionally blocking local network access.

Edge behavior—including zero allowance, fixed allocations that exceed the bundle,
free mode, resets, add-on packs, and admin overrides—MUST be specified in code-level
tests and reflected accurately in the API and dashboard. A change to these semantics
requires a regression test and a user-facing change note.

### IV. IPv4 and IPv6 Parity

Every new kernel networking feature MUST support IPv4 and IPv6. If parity is not
possible or is intentionally out of scope, the feature MUST declare the limitation
and its operational impact in Structure_README.md, and tests MUST verify the supported
and unsupported cases. Address discovery limits MUST be surfaced instead of being
presented as complete device coverage.

### V. Safe and Recoverable Host Changes

Inputs used to construct network commands or configuration MUST be validated before
they reach the host. Host changes MUST be idempotent, bounded by timeouts, and
recoverable through rollback or a documented reconciliation path when possible.

Failures in accounting or enforcement MUST be logged and exposed as an unhealthy
state. The application MUST retry or rebuild failed enforcement state, or report
clearly that enforcement is unavailable; it MUST NOT present stale or unavailable
kernel state as healthy.

### VI. Responsive Async Control Plane

Blocking subprocess, filesystem, and network operations MUST NOT run on the asyncio
event loop. Use an asynchronous interface or move blocking work to a worker thread,
with explicit timeouts and cancellation behavior where supported.

Background maintenance and monitoring failures MUST NOT silently stall API requests,
WebSocket updates, or unrelated maintenance steps. Failures that affect enforcement
MUST remain visible to the operator.

### VII. Durable State and Secret Handling

Runtime settings changed through the dashboard MUST survive application restarts,
package upgrades, and setup-script reruns. Database schema changes MUST use guarded,
forward-compatible migrations. Configuration files provide defaults; they MUST NOT
silently take ownership of a value already managed at runtime.

Credentials and private keys MUST NOT be committed, copied into logs, or exposed in
API responses. Administrative access MUST use revocable authentication, and a
factory-default credential MUST NOT authorize remote or WAN administration.

### VIII. Regression Tests at System Boundaries

Every fixed outage or security-relevant defect MUST receive a regression test.
Unit tests that exercise host networking MUST use injected fakes and MUST NOT alter
the developer or CI host. Tests for network policy MUST cover both address families
or assert the documented IPv4-only boundary.

Changes MUST pass the repository's applicable lint and test checks before merge.
Tests MUST be corrected through implementation changes; valid tests MUST NOT be
removed or weakened to obtain a passing result.

### IX. Consistent Operator Experience

User-visible dashboard text MUST use the shared translation system and keep English
and Arabic catalogs in sync. Status shown to operators MUST distinguish configured
policy from policy actually active in the kernel or host service.

CHANGELOG.md is written for end users in plain language. Structure_README.md holds
technical architecture and troubleshooting details. README.md and README_AR.md
document user setup and operation in their respective languages.

## Engineering Standards

- Keep network command construction in the owning subsystem and avoid shell-string
  interpolation for externally supplied values.
- Preserve existing user settings and database data during upgrades and migrations.
- Document any deliberate exception to these principles with its scope, impact,
  tests, and a path to resolution.
- Prefer the smallest change that satisfies the behavior while preserving the
  established module boundaries.

## Governance

### Amendment Process

Any change to a principle MUST update this constitution and its Sync Impact Report.
The change proposal MUST identify affected code, tests, documentation, and any
existing behavior that becomes nonconforming. Repository guidance and templates that
depend on the changed principle MUST be synchronized in the same change when present.

Plans and pull requests MUST include a Constitution Check that names applicable
principles and records any justified exception. An exception is limited to the
approved scope and MUST include an owner and a resolution path.

### Versioning

The constitution uses semantic versioning:

- MAJOR for removing or incompatibly redefining a principle.
- MINOR for adding a principle or materially expanding its requirements.
- PATCH for clarification, wording, or non-semantic corrections.

Ratification and amendment dates use ISO format YYYY-MM-DD. An initial draft is not
considered ratified until the project maintainer approves it.

### Compliance Review

Every implementation review MUST check the relevant principles, their regression
tests, and user-facing documentation. CI results are evidence for the checks it runs;
they do not replace review of host behavior that CI cannot exercise. Gaps found in
the reviewed scope MUST be recorded as explicit follow-up work rather than treated
as compliant by default.

# .refactor/specs/memory/constitution.md
# Project Constitution — QuotaManager Total Refactor
# Derived from MASTER PROMPT golden rules + codebase conventions

## Core Principles
1. **Behavior preserved**: default contract is behavior and public contracts preserved. Any intentional change must be (a) declared in a spec, (b) justified in `.refactor/decisions.md`, (c) committed separately with `change:` prefix. Silent behavior changes are a failure.
2. **Small steps**: one concern per task; one commit per task; conventional commits (`refactor(scope):`, `test(scope):`, `fix(scope):`, `change(scope):`, `docs:`, `chore:`).
3. **Tests first / safety net**: never delete, skip, or weaken a test to get green. Characterization tests pin current behavior before refactoring.
4. **No dependency upgrades or framework migrations** inside normal refactor tasks; make them their own spec/unit after everything else is green.
5. **Never touch `main`, never force-push, never rewrite history.** Work on branch `refactor/total`.
6. **No secrets or `.env*` committed.** No destructive commands outside the repo. No `rm -rf` on anything not created by this refactor.
7. **Bugs discovered are logged in `.refactor/bugs-found.md`**, then fixed in separate `fix:` commits with a test, or deferred. Never silently fixed inside refactor commits.
8. **Evidence before claims**: only say "done"/"passes" after running the command and seeing output. Never infer or guess APIs.

## Four Lenses (applied to every unit, in order)
A. **Code**: readability, structure, duplication, types, errors, dead code. Functions ~50 lines max; guard clauses; no magic numbers; no copy-paste (extract at 3rd repetition); no dead code/unused imports.
B. **Logic**: business rules pulled into pure testable domain functions; one source of truth per rule/state; edge cases tested (empty, null, huge, concurrent, timezone/locale).
C. **Design (UI/UX)**: centralize design tokens; merge near-duplicate components; complete states (default/hover/focus/disabled/loading/empty/error); responsive at every page; semantic HTML; accessible names/labels; keyboard nav; contrast; reduced motion.
D. **Architecture & wiring**: clear layers, one-directional dependencies, no cycles; feature modules expose small public interface; routing/API/config/logging in one obvious place; contracts stable.

## Verification Gates (G0-G7, run after every task)
- G0 build/typecheck pass
- G1 lint/format pass (no new warnings vs baseline)
- G2 unit/characterization tests pass (no fewer than baseline)
- G3 smoke: every route/page loads, no runtime/console errors, key flows work
- G4 UI parity: screenshots vs baseline within tolerance (except approved `change:` pages)
- G5 contracts unchanged: API shapes, schema, URLs, exports
- G6 diff review: only allowlisted files touched; no unrelated churn; no debug leftovers; no secrets
- G7 metrics not worse: duplication, complexity, cycles, dead code, bundle size

## Naming & Structure Conventions
- Frontend layered architecture (already implemented): config → transport → model(domain/application/api) → port → api → services → state/stores → hooks → ui → shared → app
- `core/` kept as compatibility shim (gradual migration), not refactored now
- All router pages use `ctx.hooks.*` pattern
- No `core/api.js` imports outside `core/` (enforced)
- Commits: `refactor(scope): description` per task, small and describable

## Contract-Freeze Rule
Everything public is frozen unless a spec in Phase 2 declares a compatible migration. URLs, API shapes, DB schema, CLI flags, exported package API all frozen.

## Language
- Reporting to human: Egyptian Arabic, direct and concise.
- Code, identifiers, file names, commits, docs: English (unless project docs are Arabic).

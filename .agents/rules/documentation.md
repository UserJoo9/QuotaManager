---
description: Documentation writing rules and audience targeting for QuotaManager
---

# Documentation Guidelines

- **CHANGELOG.md**:
  - Must ALWAYS be written in simple, non-technical language.
  - Explain user-facing improvements, new capabilities, and resolved bugs from the perspective of someone using the dashboard at home.
  - Omit internal programming details, stack traces, compiler/linter warnings, and low-level Linux kernel command specifics.

- **Structure_README.md**:
  - Contains all deep technical and architectural details.
  - Keep fully updated with new subsystems (e.g. sing-box VPN engine, Telegram notifications, ultra ad-blocker architecture, REST APIs, and database migrations).

- **README.md and README_AR.md**:
  - User-facing manual and quick start guide in English and Arabic.

- **Git Tags & Releases**:
  - NEVER modify or force-push existing version tags (`v*`) for minor fixes or doc updates.
  - Doing so destroys release assets and clears GitHub download counters.
  - Tags are strictly created only when issuing a new official release.


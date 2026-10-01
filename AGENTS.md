# Project Guidelines & Rules

## Documentation Style & Target Audiences

1. **CHANGELOG (`CHANGELOG.md`)**:
   - **Target Audience**: Non-technical end users.
   - **Style**: Plain, accessible language focusing on **what changed, why it matters, and how it affects the user**.
   - **Do NOT include**: Internal code details, class/function names, tracebacks, git commit hashes, deep Linux kernel/nftables/tc internals, or developer jargon.
   - Group entries into clear categories: **Added**, **Changed**, **Fixed**.

2. **Developer & Architecture Guide (`Structure_README.md`)**:
   - **Target Audience**: Developers, network engineers, and advanced sysadmins.
   - **Content**: ALL deep technical details, packet paths, kernel routing, nftables rules, tc qdisc configurations, sing-box/Clash API architecture, database schemas, background loops, REST API contracts, and troubleshooting root-cause analyses.

3. **User Guides (`README.md` & `README_AR.md`)**:
   - **Target Audience**: General users and operators.
   - **Content**: High-level overview, screenshots, installation guides, dashboard usage guides, feature overviews, and day-to-day tips in both English and Arabic.

## Git Tags & Releases Policy

- **NEVER modify or force-push existing release tags (`v*`)** for small fixes, cosmetic changes, or documentation edits.
- Modifying or re-creating existing tags resets GitHub Release assets and removes download counts/statistics.
- Version tags must ONLY be created when explicitly preparing and publishing a brand new official release version.


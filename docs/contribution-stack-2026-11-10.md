# Contribution stack, 10 November 2026

The fork main branch and all 12 contribution branches include upstream 0.5.5 at `67b598edd39de2bb36febed3eff02cc1951a17e3`. The fork preserves the React dashboard and upstream unmanaged fixed-IP device controls.

## Pull requests and order

The passkey API is submitted upstream. Dashboard PRs are drafts in the fork, each targeting its preceding contribution branch so its diff contains only that slice. Submit those changes upstream in order after their dependencies land. Do not merge the staged PRs out of order.

| Order | Contribution | PR | Target branch |
| --- | --- | --- | --- |
| 1 | Passkey authentication API | [upstream #17](https://github.com/UserJoo9/QuotaManager/pull/17) | upstream `main` |
| 2 | Add React and TypeScript build tooling and API data layer | [fork #2](https://github.com/iomarsuleiman/QuotaManager/pull/2) | `contrib/passkey-auth-api` |
| 3 | Add dashboard design tokens, themes and Arabic support | [fork #3](https://github.com/iomarsuleiman/QuotaManager/pull/3) | `contrib/react-tooling` |
| 4 | Add validated form controls and mobile editing overlays | [fork #4](https://github.com/iomarsuleiman/QuotaManager/pull/4) | `contrib/design-system` |
| 5 | Add responsive navigation, reorderable sidebar and Help | [fork #5](https://github.com/iomarsuleiman/QuotaManager/pull/5) | `contrib/forms-mobile` |
| 6 | Migrate Overview, People and device management to React | [fork #6](https://github.com/iomarsuleiman/QuotaManager/pull/6) | `contrib/navigation-help` |
| 7 | Migrate quota, daily plans and Insights to React | [fork #7](https://github.com/iomarsuleiman/QuotaManager/pull/7) | `contrib/people-overview` |
| 8 | Migrate Internet, Network, VPN and Security to React | [fork #8](https://github.com/iomarsuleiman/QuotaManager/pull/8) | `contrib/quota-insights` |
| 9 | Migrate Share and System to React | [fork #9](https://github.com/iomarsuleiman/QuotaManager/pull/9) | `contrib/network-vpn-security` |
| 10 | Add passkey sign-in and account management to the dashboard | [fork #10](https://github.com/iomarsuleiman/QuotaManager/pull/10) | `contrib/share-system` |
| 11 | Activate the React dashboard with hashed assets and recovery screens | [fork #11](https://github.com/iomarsuleiman/QuotaManager/pull/11) | `contrib/passkey-frontend` |
| 12 | Add dashboard CI, operator guides and development documentation | [fork #12](https://github.com/iomarsuleiman/QuotaManager/pull/12) | `contrib/dashboard-activation` |

## Scope and dependencies

The shared form controls precede navigation and page migrations. The renewal editor is introduced with onboarding because onboarding imports it. The daily-plan editor accompanies person/device sheets because those sheets import it. Share introduces the global incoming-transfer overlay; the separate passkey UI slice follows System. These dependency changes are why the implementation order differs from the original topic list.

Each early dashboard branch keeps the deployed legacy dashboard and builds the staged React source into `.contrib-build/`. During development, migrated routes can be reviewed through Vite; migration links use the existing Python dashboard at `http://127.0.0.1:8080/`, configurable through `VITE_QM_LEGACY_URL`. The activation PR switches production only after all pages are present, generates the complete hashed/precompressed assets, and removes retired source files and temporary migration UI.

The design branch includes the token/contrast helpers imported by `scripts/export_tokens.py`. Until activation those helpers read the preview palette; afterward they read `web/css/tokens.css`. The exporter was run successfully against the design branch.

Historical fork comparison/synchronization reports and repository-specific agent policy remain on fork main rather than being added to upstream contributions. Existing screenshots remain historical evidence; no new performance measurements or browser layout audit were made for this split.

## Verification

- Every dashboard slice passed TypeScript checking and a production build before publication. Frontend tests grew from 4 form tests to 9 navigation tests, 12 with upstream device-control regressions, and 29 in the completed dashboard.
- Full completed-stack Python suite: **951 passed, 12 platform-specific skips**, on Windows with Python 3.14.
- Completed-stack frontend suite: **29 passed**.
- Python lint passed using `python -m pyflakes api core quota tests run.py`.
- Passkey API: **8 tests passed**, including a software-authenticator ceremony, TOTP interaction, replay rejection and removed credentials.
- Dashboard/static-security activation checks: **60 passed** after the portable-gzip fix.
- Token/contrast contract: **26 passed**, including after carrying the exporter dependency through the stack.
- Compressed asset regression: all 25 gzip twins use a platform-independent OS marker and decompress to the exact source bytes. This fixes the Linux CI comparison failure caused by Windows-specific gzip headers.
- Rebuilding the completed stack produced no changes in committed `web/index.html` or `web/app` assets.
- The complete stack matches the fork dashboard and English/Arabic translation sources, allowing only whitespace normalization.
- The upstream 0.5.5 Manage/Allow controls and automatic cut-off switch have three frontend request/interaction regression tests.

Physical gateway forwarding, VPN connectivity, package installation and real biometric hardware were not exercised. GitHub-hosted CI and maintainer review remain separate from these local results.

## Publishing

The connected GitHub integration rejected upstream PR creation with HTTP 403. The authenticated GitHub CLI successfully opened upstream #17 and the 11 fork draft PRs. No upstream PR was merged, no official release was published, and no existing release tag was changed or pushed.

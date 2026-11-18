"""Keep the box's apt source pointing at the project's live apt repository.

The signed apt repository is the ``gh-pages`` branch of the project's GitHub
repository, served by GitHub Pages at ``https://<owner>.github.io/<repo>/``.
Renaming the GitHub repository moves that address and GitHub does NOT redirect
Pages sites, so every box whose ``/etc/apt/sources.list.d`` still names the old
address would silently stop receiving updates through apt.

This module fixes that from the inside (CLAUDE.md rule 8: an update must apply
itself, no manual step): when the app finds a source line naming the legacy
address, it checks that the new address really serves the signed repository
and only then rewrites the URL. The signing key is unchanged (the same
``signed-by`` keyring), so nothing else in the source line changes.

Idempotent: once nothing names the legacy address it is a no-op that touches
neither the network nor the disk. Never raises - a failure leaves the sources
exactly as they were.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

from quota.updater import LEGACY_REPO, NEW_REPO

log = logging.getLogger("quota.apt_source")

SOURCES_DIR = "/etc/apt/sources.list.d"
SOURCE_SUFFIXES = (".list", ".sources")


def pages_base(repo: str) -> str:
    """``owner/repo`` -> its GitHub Pages address (no trailing slash)."""
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}"


LEGACY_BASE = pages_base(LEGACY_REPO)
NEW_BASE = pages_base(NEW_REPO)

#: ``Fetch(url) -> text``; raises on any failure. Injected by tests.
Fetch = Callable[[str], str]


def _fetch(url: str, timeout: float = 8.0) -> str:
    if not url.startswith("https://") or "@" in url.split("/")[2]:
        raise ValueError(f"refusing apt probe URL {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": "QuotaManager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read(65536).decode("utf-8", errors="replace")


def _legacy_pattern(legacy_base: str) -> "re.Pattern[str]":
    # the host is case-insensitive; a trailing slash is optional; never match a
    # longer name that merely starts the same ("QuotaManager2")
    return re.compile(re.escape(legacy_base) + r"(?![A-Za-z0-9_.-])", re.I)


def _source_files(sources_dir: str) -> list[Path]:
    d = Path(sources_dir)
    try:
        return sorted(p for p in d.iterdir()
                      if p.is_file() and p.name.endswith(SOURCE_SUFFIXES))
    except OSError:
        return []


def _new_repo_serves_apt(new_base: str, fetch: Fetch) -> bool:
    """The new address answers with a signed-repository ``Release`` file."""
    try:
        text = fetch(new_base + "/dists/stable/Release")
    except Exception as exc:  # noqa: BLE001 — not live yet (or offline)
        log.debug("apt source: %s not serving yet (%s)", new_base, exc)
        return False
    return "Codename: stable" in text


def _rewrite(path: Path, pattern: "re.Pattern[str]", new_base: str) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
        updated = pattern.sub(new_base, raw)
        if updated == raw:
            return False
        st = path.stat()
        fd, tmp = tempfile.mkstemp(prefix=".qm-apt-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(updated)
            os.chmod(tmp, st.st_mode & 0o777)
            os.replace(tmp, path)   # atomic: apt never sees a half-written file
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except (OSError, UnicodeError) as exc:
        log.warning("apt source: could not update %s: %s", path, exc)
        return False


def migrate_apt_source(*, sources_dir: str = SOURCES_DIR,
                       legacy_base: str = LEGACY_BASE,
                       new_base: str = NEW_BASE,
                       fetch: Fetch | None = None) -> dict[str, object]:
    """Point apt source files at ``new_base`` once it is live.

    Returns ``{"changed": bool, "files": [...], "reason": str}`` where reason
    is ``not_configured`` (nothing names the legacy address - the common case,
    no network), ``new_repo_not_live`` (kept the legacy address) or
    ``migrated``.
    """
    pattern = _legacy_pattern(legacy_base)
    targets: list[Path] = []
    for path in _source_files(sources_dir):
        try:
            if pattern.search(path.read_text(encoding="utf-8")):
                targets.append(path)
        except (OSError, UnicodeError):
            continue
    if not targets:
        return {"changed": False, "files": [], "reason": "not_configured"}
    if not _new_repo_serves_apt(new_base, fetch or _fetch):
        return {"changed": False, "files": [], "reason": "new_repo_not_live"}
    changed = [str(p) for p in targets if _rewrite(p, pattern, new_base)]
    if changed:
        log.info("apt source now points at %s (%s)", new_base, ", ".join(changed))
    return {"changed": bool(changed), "files": changed,
            "reason": "migrated" if changed else "not_configured"}

"""Self-update support: check the GitHub repo for a newer release, surface the
new versions' changelog, and (optionally) install the new .deb.

The version factor is the app version in ``quota/version.py``. Every network
call is a blocking stdlib fetch that MUST be run off the event loop
(``asyncio.to_thread``) — mirroring ``quota/dns_rules.fetch_url``. Install runs
under a transient systemd unit so the package's ``prerm`` (which stops the
``quota-gateway`` service) cannot kill the install: ``systemctl stop`` kills the
service's cgroup, and a plain child ``apt-get`` would die with it mid-install.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import re
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from quota.version import __version__

log = logging.getLogger("quota.updater")

#: GitHub repo holding the releases (owner/repo). Releases carry the built
#: ``quota-manager_<version>_all.deb``; the repo's ``CHANGELOG.md`` supplies
#: the per-version notes (both are the release pipeline's outputs).
DEFAULT_REPO = "UserJoo9/QuotaManager"

#: release lookup (the GitHub API's ``/releases/latest`` endpoint)
def _releases_api(repo: str) -> str:
    return f"https://api.github.com/repos/{repo}/releases/latest"


def _raw_changelog(repo: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/main/CHANGELOG.md"


def parse_version(v: str) -> tuple[int, ...]:
    """Numeric-dotted version -> comparable tuple (``"0.2.0"`` -> ``(0,2,0)``).

    Non-numeric suffixes (``-rc1`` etc.) just extend the tuple; a version that
    carries no digits at all parses to ``()`` (never a valid release version).
    """
    return tuple(int(x) for x in re.findall(r"\d+", str(v)))


def _gt(a: str, b: str) -> bool:
    return parse_version(a) > parse_version(b)


def parse_changelog(text: str, current: str, latest: str,
                    ) -> list[dict[str, str]]:
    """All released-version sections newer than ``current`` (up to ``latest``)
    from a CHANGELOG.md, newest first.

    Sections are the ``## [<version>]`` headers (``## [Unreleased]`` is
    skipped — it is not a shipped version). A very old ``current`` yields many
    entries: the caller renders them in a scroll frame. Each entry is
    ``{version, title, body}`` with the body the raw section text (its own
    ``###`` sub-headings preserved).
    """
    # re.split with a capturing group returns [pre, hdr, body, hdr, body, ...]
    # A header may carry a " — YYYY-MM-DD" date suffix ("## [0.2.1] —
    # 2026-08-17") — the trailing group tolerates it but stays on the SAME
    # line (never eating the following section's header via \s); non-version
    # and Unreleased titles are filtered below anyway.
    parts = re.split(r"^##\s+\[([^\]]+)\](?:[ \t]+[^\n]*)?$", text, flags=re.M)
    sections: list[tuple[str, str]] = []
    for i in range(1, len(parts) - 1, 2):
        title = parts[i].strip()
        body = parts[i + 1].strip()
        if title.lower() == "unreleased":
            continue
        if not parse_version(title) or not _gt(title, current) \
                or _gt(title, latest):
            continue
        sections.append((title, body))
    sections.sort(key=lambda t: parse_version(t[0]), reverse=True)
    return [{"version": v, "title": f"v{v}", "body": b} for v, b in sections]


# -- default blocking implementations (off the event loop) -------------------

#: SSRF allowlist — the ONLY hosts the updater may fetch. Everything else
#: (an attacker-influenced repo string, a DNS-rebinding trick, a crafted
#: release asset URL) is refused before any connection is made.
_GITHUB_HOSTS = frozenset({
    "api.github.com",
    "raw.githubusercontent.com",
    "github.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
})

#: Private / loopback prefixes the allowlist refuses regardless of hostname.
_PRIVATE_NETS = ("127.", "10.", "192.168.", "169.254.", "0.", "::1", "fe80:")


def _assert_safe_github_url(url: str) -> None:
    """Raise ValueError unless ``url`` is an https GitHub-host URL.

    Guards every fetch in this module against SSRF: the host must be on the
    allowlist, the scheme must be https, and the host must not resolve to a
    private/loopback address. Raises instead of fetching so a bad value is a
    loud failure, never a silent connection to an internal service.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https",):
        raise ValueError(f"refusing non-https update URL: {url!r}")
    if parsed.username or parsed.password:
        raise ValueError("refusing update URL with embedded credentials")
    host = (parsed.hostname or "").lower()
    if host not in _GITHUB_HOSTS:
        raise ValueError(f"update host {host!r} not in allowlist")
    if host.startswith(_PRIVATE_NETS) or host == "localhost":
        raise ValueError(f"update host {host!r} is private/loopback")


def _fetch_json(url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch + parse a JSON document (the GitHub API). Raises on failure."""
    _assert_safe_github_url(url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "QuotaManager",
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.load(resp)


def _fetch_text(url: str, timeout: float = 30.0) -> str:
    """Fetch a UTF-8 text document (raw CHANGELOG.md). Raises on failure."""
    _assert_safe_github_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "QuotaManager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _download(url: str, dest: str, timeout: float = 120.0) -> None:
    """Download a release asset (.deb) to ``dest``. Raises on failure."""
    _assert_safe_github_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "QuotaManager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(dest, "wb") as fh:  # noqa: S310
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def _run_command(argv: list[str], timeout: float = 15.0) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output). Raises on
    subprocess-level failure (missing binary, timeout)."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# -- the manager -------------------------------------------------------------

class Updater:
    """Version-check + optional auto-install, owned by the Gateway.

    State is persisted in the ``updates_state`` settings row (checked_at,
    latest_version, changelog, last error) so a restart neither re-fires the
    notification nor re-checks within the interval. ``current_version`` is the
    running app's version from ``quota/version.py``.
    """

    #: default check cadence (a release check once a day is plenty)
    DEFAULT_INTERVAL_SEC = 24 * 3600

    def __init__(self, database: Any, repo: str = DEFAULT_REPO,
                 current_version: str = __version__,
                 interval_sec: float = DEFAULT_INTERVAL_SEC,
                 fetch_json: Callable[..., dict[str, Any]] | None = None,
                 fetch_text: Callable[..., str] | None = None,
                 download: Callable[..., None] | None = None,
                 run_command: Callable[..., tuple[int, str]] | None = None
                 ) -> None:
        self.database = database
        self.repo = repo
        self.current_version = current_version
        self.interval_sec = interval_sec
        self._fetch_json = fetch_json or _fetch_json
        self._fetch_text = fetch_text or _fetch_text
        self._download = download or _download
        self._run_command = run_command or _run_command
        self._checking = False
        self._installing = False
        self._install_lock = asyncio.Lock()

    # -- persisted helpers ---------------------------------------------------

    async def _get_state(self) -> dict[str, Any]:
        raw = await self.database.get_setting("updates_state", "{}")
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    async def _enabled(self) -> bool:
        return await self.database.get_setting("updates_enabled", "1") == "1"

    async def _auto_install(self) -> bool:
        return await self.database.get_setting(
            "updates_auto_install", "") == "1"

    async def set_enabled(self, enabled: bool) -> None:
        """Toggle the automatic check. Turning it ON clears a stale last-error
        so the card never resurrects an old failure — the next check reports
        fresh (a disabled box that failed once would otherwise show that error
        forever)."""
        await self.database.set_setting("updates_enabled",
                                        "1" if enabled else "")
        if enabled:
            s = await self._get_state()
            if s.get("error"):
                s["error"] = ""
                await self.database.set_setting("updates_state", json.dumps(s))

    # -- public surface ------------------------------------------------------

    async def state(self) -> dict[str, Any]:
        """The Admin-tab / snapshot view of the updater (never raises)."""
        s = await self._get_state()
        current = self.current_version
        latest = s.get("latest_version", "")
        available = bool(latest) and _gt(latest, current)
        enabled = await self._enabled()
        return {
            "enabled": enabled,
            "auto_install": await self._auto_install(),
            "current_version": current,
            "latest_version": latest,
            "available": available,
            "checked_at": s.get("checked_at", ""),
            # a disabled box reports no error — the record stays in the DB but
            # the card must never surface a stale failure (it tells the admin
            # to enable checks instead)
            "error": "" if not enabled else s.get("error", ""),
            "changelog": s.get("changelog", []) if available else [],
            "checking": self._checking,
            "installing": self._installing,
            "last_install": s.get("last_install", ""),
        }

    async def maybe_check(self) -> None:
        """Run a check when due (interval elapsed AND enabled). No-op otherwise
        — called from the maintenance tick, so a failure here must never
        disturb the loop (check_now catches everything)."""
        if not await self._enabled():
            return
        s = await self._get_state()
        try:
            last = _dt.datetime.fromisoformat(
                s.get("checked_at", "")).timestamp()
        except (ValueError, TypeError):
            last = 0.0
        if time.time() - last < self.interval_sec:
            return
        await self.check_now()

    async def check_now(self) -> dict[str, Any]:
        """Force a check against GitHub. Persists the result (checked_at +
        latest/changelog or the error) and returns :meth:`state`. Fires the
        auto-install when armed and a newer version is found.

        Refuses to dial GitHub when the "check automatically" toggle is OFF —
        that switch is the master; a disabled box must not fetch (or re-persist
        a failure), the card tells the admin to enable it."""
        if not await self._enabled():
            return await self.state()
        if self._checking:
            return await self.state()
        self._checking = True
        try:
            release = await asyncio.to_thread(
                self._fetch_json, _releases_api(self.repo))
            latest = str(release.get("tag_name", "") or "").lstrip("v")
            if not parse_version(latest):
                raise ValueError(f"unexpected release tag {latest!r}")
            s = await self._get_state()
            changelog: list[dict[str, str]] = []
            if _gt(latest, self.current_version):
                try:
                    text = await asyncio.to_thread(
                        self._fetch_text, _raw_changelog(self.repo))
                    changelog = parse_changelog(
                        text, self.current_version, latest)
                except Exception:  # noqa: BLE001 — notes must not lose the flag
                    notes = (release.get("body") or "").strip()
                    if notes:
                        changelog = [{"version": latest,
                                      "title": f"v{latest}", "body": notes}]
            s.update({"checked_at": _now_iso(), "latest_version": latest,
                      "error": "", "changelog": changelog})
            await self.database.set_setting("updates_state", json.dumps(s))
            if _gt(latest, self.current_version):
                await self.database.add_event(
                    f"Update available: v{self.current_version} → v{latest}",
                    "info")
                if await self._auto_install():
                    try:
                        asyncio.get_running_loop().create_task(
                            self.install_latest())
                    except RuntimeError:  # no running loop (sync context)
                        pass
            return await self.state()
        except Exception as exc:  # noqa: BLE001 — a network error must never 500
            log.warning("update check failed: %s", exc)
            s = await self._get_state()
            s.update({"checked_at": _now_iso(),
                      "error": str(exc)[:300]})  # keep the last known latest
            await self.database.set_setting("updates_state", json.dumps(s))
            return await self.state()
        finally:
            self._checking = False

    async def install_latest(self) -> dict[str, Any]:
        """Download + install the latest .deb (when one is available).

        Serialized (``_install_lock``) so a manual "Install now" and an
        auto-install never double-run. The install runs under a transient
        systemd unit that survives the service being stopped by the package's
        ``prerm``; the box restarts the new version via ``postinst``.
        """
        async with self._install_lock:
            if self._installing:
                return {"ok": False, "reason": "already installing"}
            self._installing = True
            try:
                st = await self.state()
                latest = st["latest_version"]
                if not st["available"]:
                    return {"ok": False, "reason": "no update available"}
                release = await asyncio.to_thread(
                    self._fetch_json, _releases_api(self.repo))
                deb_url = self._find_deb_url(release, latest)
                if not deb_url:
                    return {"ok": False,
                            "reason": "no .deb asset in the release"}
                dest = f"/tmp/quota-manager_{latest}.deb"
                await asyncio.to_thread(self._download, deb_url, dest)
                ok, out = await asyncio.to_thread(
                    self._install_deb, dest)
                if not ok:
                    log.error("update install failed: %s", out[-2000:])
                    return {"ok": False, "reason": "install failed (see logs)"}
                s = await self._get_state()
                s["last_install"] = _now_iso()
                s["latest_version"] = ""  # the running version catches up
                await self.database.set_setting("updates_state", json.dumps(s))
                await self.database.add_event(
                    f"Update v{latest} installed — gateway restarting", "info")
                return {"ok": True, "version": latest}
            except Exception as exc:  # noqa: BLE001
                log.exception("update install failed")
                return {"ok": False, "reason": str(exc)[:300]}
            finally:
                self._installing = False

    # -- install plumbing ----------------------------------------------------

    def _find_deb_url(self, release: dict[str, Any], latest: str) -> str:
        for asset in release.get("assets") or []:
            if (asset.get("name") or "").endswith(".deb"):
                return asset.get("browser_download_url") or ""
        # the release pipeline names the asset deterministically; fall back to
        # the constructed URL when the API's asset list is empty
        return f"https://github.com/{self.repo}/releases/download/" \
               f"v{latest}/quota-manager_{latest}_all.deb"

    def _install_deb(self, deb_path: str) -> tuple[bool, str]:
        """Install ``deb_path`` outside the service's cgroup.

        Prefers a transient systemd unit (``systemd-run``) so the package's
        ``prerm``/``postinst`` — which stop/start the ``quota-gateway``
        service, killing our whole cgroup — cannot kill the install midway.
        Falls back to a plain ``apt-get`` when systemd-run is absent.
        """
        argv = ["systemd-run", "--unit=quota-update-install", "--collect",
                "--quiet", "--wait", "--property=Type=oneshot",
                "--property=Environment=DEBIAN_FRONTEND=noninteractive",
                "apt-get", "install", "-y", deb_path]
        try:
            rc, out = self._run_command(argv, timeout=300.0)
        except (OSError, subprocess.SubprocessError):
            rc, out = 1, "systemd-run unavailable"
        if rc == 0:
            return True, out
        if "not found" in out or rc == 1:
            # no systemd-run — plain foreground apt-get (tests / non-systemd)
            try:
                rc, out = self._run_command(
                    ["apt-get", "install", "-y", deb_path], timeout=300.0)
            except (OSError, subprocess.SubprocessError) as exc:
                return False, str(exc)
        return rc == 0, out


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")
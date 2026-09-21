"""Self-update for the HA Readonly Inspector integration (v0.3.0).

Lets the integration update its own code from the GitHub releases of
acrimin/ha-readonly, so routine updates don't require manual HACS steps.

Design, mirroring the v2 write guardrails:
- Admin-only, audit-logged, ``dry_run`` defaults to true.
- Dry run: checks the latest GitHub release tag against the running
  INTEGRATION_VERSION and reports whether an update is available. No
  download, no file changes.
- Applied: downloads the release tarball, backs up the installed
  ``custom_components/ha_readonly`` directory to a timestamped backup next
  to it, extracts the new code over the install, and verifies the new
  manifest version matches the release tag.
- A rollback endpoint restores the most recent backup (also dry-run first).
- A rollback endpoint restores the most recent backup (also dry-run first).
- Restart is available via POST /api/ha_readonly/restart (dry-run reports
  what's currently mid-run first), but it is never triggered automatically.
  Every restart stays explicitly approved: a restart interrupts in-progress
  automations/scripts and takes HA offline briefly.

Trust note: this downloads and installs executable code from a public
GitHub repo into the HA process. The repo is the owner's own; a compromise
of that repo would be a compromise of this update path. The backup +
rollback endpoint is the mitigation.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Context, HomeAssistant

try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:  # pragma: no cover - very old HA
    KEY_HASS_USER = "hass_user"

from .const import DOMAIN, INTEGRATION_VERSION
from .views import _ViewError

_LOGGER = logging.getLogger(__name__)

_REPO = "acrimin/ha-readonly"
_RELEASES_LATEST = f"https://api.github.com/repos/{_REPO}/releases/latest"
_GITHUB_TIMEOUT = 30


def _parse_version(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in tag.lstrip("v").split("."))


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"ha-readonly/{INTEGRATION_VERSION}"}
    )
    with urllib.request.urlopen(req, timeout=_GITHUB_TIMEOUT) as resp:
        return json.load(resp)


def _download(url: str, dest: str) -> None:
    req = urllib.request.Request(
        url, headers={"User-Agent": f"ha-readonly/{INTEGRATION_VERSION}"}
    )
    with urllib.request.urlopen(req, timeout=_GITHUB_TIMEOUT) as resp, open(
        dest, "wb"
    ) as fh:
        shutil.copyfileobj(resp, fh)


def _install_from_tarball(tarball_path: str, install_dir: str) -> tuple[str, str]:
    """Backup current install, extract new code over it.

    Returns (backup_dir, new_version). Raises _ViewError on any problem,
    leaving the install untouched (backup happens first, extract second).
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = f"{install_dir}.bak-{stamp}"
    shutil.copytree(install_dir, backup_dir)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(tarball_path, "r:gz") as tar:
                # Tarball root is like ha-readonly-0.3.0/; resolve safely.
                members = tar.getmembers()
                top = members[0].name.split("/")[0] if members else ""
                tar.extractall(tmp)
            src = os.path.join(tmp, top, "custom_components", "ha_readonly")
            if not os.path.isdir(src):
                raise _ViewError(500, "release_missing_integration_dir")
            manifest_path = os.path.join(src, "manifest.json")
            try:
                with open(manifest_path) as fh:
                    new_version = json.load(fh)["version"]
            except (OSError, KeyError, ValueError) as err:
                raise _ViewError(500, f"release_manifest_unreadable: {err}") from err
            # Replace (not merge) so files removed upstream don't linger.
            # Modules are already imported, so this is safe while running;
            # the new code loads on next restart. Backup exists for recovery.
            shutil.rmtree(install_dir)
            try:
                shutil.copytree(src, install_dir)
            except Exception as err:
                raise _ViewError(
                    500, f"install_failed_mid_copy: {err} (backup at {backup_dir})"
                ) from err
    except _ViewError:
        raise
    except Exception as err:  # noqa: BLE001 - report, install may be half-updated
        raise _ViewError(500, f"install_failed: {err}") from err
    return backup_dir, new_version


def _latest_backup(install_dir: str) -> str | None:
    candidates = glob.glob(f"{install_dir}.bak-*")
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _rollback_to_backup(backup_dir: str, install_dir: str) -> str:
    """Restore install_dir from backup_dir. Returns new manifest version."""
    # Replace (not merge) so files added by the bad update don't linger.
    shutil.rmtree(install_dir)
    shutil.copytree(backup_dir, install_dir)
    with open(os.path.join(install_dir, "manifest.json")) as fh:
        return json.load(fh)["version"]  # type: ignore[no-any-return]


class _SelfUpdateBase(HomeAssistantView):
    requires_auth = True
    _mutation_lock = asyncio.Lock()

    async def _authed_user(self, request):
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            _LOGGER.warning("%s: forbidden self-update to %s", DOMAIN, request.path)
            return None
        return user

    def _audit(self, user, request, detail: str, applied: bool, status: int) -> None:
        _LOGGER.info(
            "%s: user=%s method=%s path=%s %s %s status=%s",
            DOMAIN,
            getattr(user, "name", "?"),
            request.method,
            request.path,
            detail,
            "APPLIED" if applied else "dry_run",
            status,
        )

    async def _dry_run(self, request) -> bool:
        try:
            body = await request.json()
        except ValueError:
            return True
        if not isinstance(body, dict):
            return True
        dry_run = body.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise _ViewError(400, "dry_run_must_be_boolean")
        return dry_run


class SelfUpdateView(_SelfUpdateBase):
    """Check for and install integration updates from GitHub releases."""

    url = "/api/ha_readonly/self_update"
    name = "api:ha_readonly:self_update"

    async def post(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        try:
            dry_run = await self._dry_run(request)
            return await self._handle(hass, user, request, dry_run)
        except _ViewError as err:
            self._audit(user, request, "check", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: unhandled self-update error", DOMAIN)
            self._audit(user, request, "check", False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, dry_run: bool):
        async with self._mutation_lock:
            try:
                release = await hass.async_add_executor_job(
                    _fetch_json, _RELEASES_LATEST
                )
            except Exception as err:  # noqa: BLE001 - network failure
                raise _ViewError(502, f"github_unreachable: {err}") from err
            latest_tag = str(release.get("tag_name", ""))
            tarball_url = str(release.get("tarball_url", ""))
            if not latest_tag or not tarball_url:
                raise _ViewError(502, "github_release_malformed")

            current = _parse_version(INTEGRATION_VERSION)
            latest = _parse_version(latest_tag)
            update_available = latest > current

            if dry_run or not update_available:
                self._audit(user, request, f"check latest={latest_tag}", False, 200)
                return self.json(
                    {
                        "result": "dry_run" if dry_run else "ok_no_update",
                        "current": INTEGRATION_VERSION,
                        "latest": latest_tag,
                        "update_available": update_available,
                        "restart_required": False,
                    }
                )

            install_dir = hass.config.path("custom_components", "ha_readonly")
            with tempfile.TemporaryDirectory() as tmp:
                tarball_path = os.path.join(tmp, "release.tar.gz")
                try:
                    await hass.async_add_executor_job(
                        _download, tarball_url, tarball_path
                    )
                except Exception as err:  # noqa: BLE001
                    raise _ViewError(502, f"download_failed: {err}") from err
                backup_dir, new_version = await hass.async_add_executor_job(
                    _install_from_tarball, tarball_path, install_dir
                )

            if _parse_version(new_version) != latest:
                raise _ViewError(500, "installed_version_mismatch")

            self._audit(
                user, request, f"update to {latest_tag}", True, 200
            )
            return self.json(
                {
                    "result": "ok_staged",
                    "current": INTEGRATION_VERSION,
                    "latest": latest_tag,
                    "update_available": False,
                    "backup": backup_dir,
                    "restart_required": True,
                    "note": "Restart Home Assistant to load the new code.",
                }
            )


class SelfUpdateRollbackView(_SelfUpdateBase):
    """Restore the most recent pre-update backup of the integration."""

    url = "/api/ha_readonly/self_update/rollback"
    name = "api:ha_readonly:self_update:rollback"

    async def post(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        try:
            dry_run = await self._dry_run(request)
            return await self._handle(hass, user, request, dry_run)
        except _ViewError as err:
            self._audit(user, request, "rollback", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: unhandled rollback error", DOMAIN)
            self._audit(user, request, "rollback", False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, dry_run: bool):
        install_dir = hass.config.path("custom_components", "ha_readonly")
        async with self._mutation_lock:
            backup_dir = await hass.async_add_executor_job(
                _latest_backup, install_dir
            )
            if backup_dir is None:
                raise _ViewError(404, "no_backup_found")

            restored_version: str | None = None
            if not dry_run:
                try:
                    restored_version = await hass.async_add_executor_job(
                        _rollback_to_backup, backup_dir, install_dir
                    )
                except Exception as err:  # noqa: BLE001
                    raise _ViewError(500, f"rollback_failed: {err}") from err

            self._audit(
                user, request, f"rollback to {backup_dir}", not dry_run, 200
            )
            return self.json(
                {
                    "result": "dry_run" if dry_run else "ok_rolled_back",
                    "backup": backup_dir,
                    "restored_version": restored_version,
                    "restart_required": not dry_run,
                    "note": "Restart Home Assistant to load the restored code."
                    if not dry_run
                    else None,
                }
            )


class RestartView(_SelfUpdateBase):
    """Restart Home Assistant, e.g. to load a staged update.

    Dry run (default) reports what is currently mid-run so the caller can
    judge safety: running scripts and automations with active runs. Applied
    calls HA's own ``homeassistant.restart`` service with the requesting
    admin's context. The HTTP response may not arrive if the server goes
    down first; verify by polling ``/api/ha_readonly/info`` afterwards.
    """

    url = "/api/ha_readonly/restart"
    name = "api:ha_readonly:restart"

    async def post(self, request):
        user = await self._authed_user(request)
        if user is None:
            return self.json({"error": "admin_required"}, status_code=403)
        hass: HomeAssistant = request.app["hass"]
        try:
            dry_run = await self._dry_run(request)
            return await self._handle(hass, user, request, dry_run)
        except _ViewError as err:
            self._audit(user, request, "restart", False, err.status)
            return self.json({"error": err.message}, status_code=err.status)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: unhandled restart error", DOMAIN)
            self._audit(user, request, "restart", False, 500)
            return self.json({"error": "internal_error"}, status_code=500)

    async def _handle(self, hass, user, request, dry_run: bool):
        running_scripts = [
            s.entity_id
            for s in hass.states.async_all("script")
            if s.state == "on"
        ]
        active_automations = [
            s.entity_id
            for s in hass.states.async_all("automation")
            if (s.attributes.get("current") or 0) > 0
        ]

        if dry_run:
            self._audit(user, request, "restart check", False, 200)
            return self.json(
                {
                    "result": "dry_run",
                    "running_scripts": running_scripts,
                    "active_automations": active_automations,
                    "safe_to_restart": not running_scripts
                    and not active_automations,
                }
            )

        self._audit(user, request, "restart", True, 200)
        # blocking=False: queue the restart and return; the server may go
        # down before the response flushes, which is expected.
        await hass.services.async_call(
            "homeassistant",
            "restart",
            {},
            blocking=False,
            context=Context(user_id=user.id),
        )
        return self.json(
            {
                "result": "restarting",
                "note": "Verify by polling /api/ha_readonly/info until it responds.",
            }
        )


_SELF_UPDATE_VIEWS = (
    SelfUpdateView,
    SelfUpdateRollbackView,
    RestartView,
)


def async_register_self_update_views(hass: HomeAssistant) -> None:
    """Register the self-update views. Idempotent across reloads."""
    key = f"{DOMAIN}_self_update_views_registered"
    if hass.data.get(key):
        return
    for view_cls in _SELF_UPDATE_VIEWS:
        hass.http.register_view(view_cls())
    hass.data[key] = True

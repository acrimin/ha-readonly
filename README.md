# HA Readonly Inspector

An admin-only HTTP API for inspecting Home Assistant configuration — and, as
of v2, making guardrailed changes to automations; v5 adds guardrailed script
writes and device/entity renames. The repo name is historical: v1 was
strictly read-only; v2+ add writes that reuse the exact save path the HA
frontend itself uses.

> **v0.6.3 note:** v0.6.2 fixed the backup-location bug; v0.6.3 fixes the /logs endpoint 404 (its route `name` did not follow the integration’s convention, so aiohttp never matched it).
>
> **v0.6.2 note (superseded):** the v0.5.0 startup failure is diagnosed — it was never the
> v0.5.0 code. The self-update backup directory
> (`custom_components/ha_readonly.bak-<timestamp>/`, a full copy including
> `manifest.json`) was discovered by HA's integration loader, which fatally
> failed trying to import it as `custom_components.ha_readonly.bak-...`.
> This release keeps the full v0.5.0 feature set (script writes, renames)
> plus the read-only `/logs` endpoint, and moves self-update backups to
> `<config>/ha_readonly_backups/` where the loader never sees them. If you
> are stuck on the broken v0.5.0 install, delete any
> `custom_components/ha_readonly.bak-*` directories and restart.

## What it exposes

### v1 — read-only (GET)

| Endpoint | What you get |
|---|---|
| `/api/ha_readonly/info` | Integration version, HA version, endpoint list |
| `/api/ha_readonly/automations` | Automation summaries (id, name, state, last triggered) |
| `/api/ha_readonly/automations/{id}` | Full raw config for one automation |
| `/api/ha_readonly/scripts` | Script summaries |
| `/api/ha_readonly/scripts/{id}` | Full raw config for one script |
| `/api/ha_readonly/scenes` | Scene summaries |
| `/api/ha_readonly/scenes/{id}` | Scene config (best effort) |
| `/api/ha_readonly/registries/entities` | Entity registry dump |
| `/api/ha_readonly/registries/devices` | Device registry dump |
| `/api/ha_readonly/registries/areas` | Area registry dump |
| `/api/ha_readonly/states?domain=&limit=` | Entity states (bounded, default 1000) |
| `/api/ha_readonly/repairs` | Active repair issues (metadata only) |
| `/api/ha_readonly/logs?lines=&level=&search=` | Tail + filter home-assistant.log (admin, read-only) |

`{id}` accepts an entity id (`automation.foo`) or a unique id.

### v2 — guardrailed automation writes (POST)

| Endpoint | What it does |
|---|---|
| `POST /api/ha_readonly/automations/write/{key}` | Create or update one automation (upsert) |
| `POST /api/ha_readonly/automations/write/{key}/delete` | Delete one automation |

`{key}` is the automation's unique id (the `id:` field in `automations.yaml`).

Request body for create/update:

```json
{
  "dry_run": true,
  "automation": {
    "alias": "My automation",
    "description": "...",
    "triggers": [ ... ],
    "conditions": [ ... ],
    "actions": [ ... ],
    "mode": "single"
  }
}
```

Request body for delete: `{ "dry_run": true }` (body optional).

### v5 — guardrailed script writes and renames (POST)

| Endpoint | What it does |
|---|---|
| `POST /api/ha_readonly/scripts/write/{key}` | Create or update one script (upsert) |
| `POST /api/ha_readonly/scripts/write/{key}/delete` | Delete one script |
| `POST /api/ha_readonly/devices/rename` | Rename a device and/or one entity's friendly name |

`{key}` is the script's object id (the key in `scripts.yaml`).

Request body for script create/update:

```json
{
  "dry_run": true,
  "script": {
    "alias": "My script",
    "description": "...",
    "sequence": [ ... ]
  }
}
```

Request body for script delete: `{ "dry_run": true }` (body optional).

Request body for rename:

```json
{
  "dry_run": true,
  "device_id": "<device registry id>",
  "device_name": "Sound machine",
  "entity_id": "switch.outlet",
  "entity_name": "Sound machine"
}
```

At least one of `device_name` / `entity_name` must be present (each with
its id). A `null` name clears the custom name, exactly like the HA UI.
Dry-run is a pure no-op that still returns the would-be before/after for
each rename.

## How writes stay safe

The write path is deliberately the same one the HA frontend uses, verified
against HA 2026.6.4 source (`homeassistant/components/config/view.py`,
`homeassistant/components/config/automation.py`,
`homeassistant/components/config/script.py`,
`homeassistant/components/config/device_registry.py`,
`homeassistant/components/config/entity_registry.py`):

1. **Same file.** UI-created automations are written to `automations.yaml`
   (a list); UI-created scripts are written to `scripts.yaml` (a dict keyed
   by object id); this integration writes to those exact files. No
   `.storage` access for YAML writes.
2. **Same validation.** Every config is validated with HA's own
   `async_validate_config_item` (automation's and script's respectively)
   before anything touches disk. Invalid configs are rejected with 400 and
   nothing is written. Script keys must be valid slugs, like the UI
   requires.
3. **Same atomic write.** YAML is dumped before the file is opened and
   written atomically, under a mutation lock.
4. **Same reload.** After an applied automation create/update, only that
   one automation is reloaded via `automation.reload` with `{id: key}`.
   Scripts have no per-item reload in HA, so the UI reloads *all* scripts
   via `script.reload` — this integration does the same. Deletes remove the
   entity-registry entry, exactly like the UI (no reload).
5. **Same renames.** Device renames call
   `device_registry.async_update_device(device_id, name_by_user=...)` and
   entity renames call
   `entity_registry.async_update_entity(entity_id, name=...)` — the exact
   calls behind the HA UI's rename dialogs. Registry-backed, so no YAML
   backup applies; HA persists `.storage` atomically itself.

On top of that, this integration adds:

- **`dry_run` defaults to `true`.** Nothing is written, backed up,
  reloaded, or renamed unless the caller explicitly passes
  `"dry_run": false`.
- **Backup before every applied YAML write.** The file is copied to a
  timestamped `.bak` file next to the original first.
- **Diffs.** Every response includes a unified diff of the item's YAML
  (current vs proposed) for review before applying; renames return
  before/after for each target.
- **Audit logging.** Every write attempt is logged with user, key,
  applied/dry-run, and status. Request bodies are never logged.

Honest limits: validation is syntactic — a config can pass HA's schema and
still do the wrong thing (wrong light, runaway loop). The dry-run diff plus
explicit human approval is the mitigation; nothing in code can replace that
review. Scope is automations, scripts, and renames only; scenes remain
read-only. A script reload touches every script (HA limitation, same as the
UI), so avoid saving scripts while a long-running script is mid-flight.

### v3 — self-update (POST)

| Endpoint | What it does |
|---|---|
| `POST /api/ha_readonly/self_update` | Check GitHub releases; optionally download and stage the latest |
| `POST /api/ha_readonly/self_update/rollback` | Restore the most recent pre-update backup |
| `POST /api/ha_readonly/restart` | Restart Home Assistant (dry-run reports what's mid-run first) |

Request body: `{ "dry_run": true }` (default true).

- Dry run reports `current`, `latest`, and `update_available`. Nothing is
  downloaded.
- Applied (only when a newer release exists): downloads the release tarball,
  backs up `custom_components/ha_readonly` to a timestamped directory next
  to it, replaces the install with the new code, and verifies the new
  manifest version matches the release tag.
- Returns `restart_required: true` — new code only loads after a restart.
  Restart via `POST /api/ha_readonly/restart`: dry-run lists running
  scripts and active automations first so you can judge safety. Restarts
  are never triggered automatically; every one stays explicitly approved.
- Rollback restores the most recent backup the same way (dry-run first).

Trust note: this installs executable code from the owner's public GitHub
repo into the HA process. Backup + rollback is the mitigation if a release
is bad.

## Security model

- Every endpoint requires an authenticated **admin** user (normal HA auth).
  Non-admin requests get 403; unauthenticated get 401.
- v1 has no write path: no service calls, no file reads, no state changes.
- v2+ write endpoints are admin-only, dry-run by default, validated, backed
  up, and audit-logged as described above.
- Every request is audit-logged (user, method, path, status). Tokens and
  request bodies are never logged.
- All reads come from Home Assistant's in-memory helpers.

## Install (via HACS)

1. HACS → ⋮ → Custom repositories → add this repo URL, category Integration.
2. Install "HA Readonly Inspector", restart Home Assistant.
3. Settings → Devices & Services → Add Integration → HA Readonly Inspector.

## Removal

Delete the integration, remove it from HACS, restart. HTTP views are
process-global and unload fully on restart. Audit log lines written before
removal remain in the HA log.

## Compatibility

Reviewed against HA 2026.6.4 and 2026.7.2 source. v1 read endpoints tested on
a live 2026.6.4 instance. v2 write endpoints are new in 0.2.0 and not yet
tested on a live instance. v3 self-update is new in 0.3.0; its install and
rollback file logic is unit-tested, but it has not yet run on a live
instance. v4 restart endpoint is new in 0.4.0 and likewise untested live.
v5 script-write and rename endpoints are new in 0.5.0; their file and
request-validation logic is unit-tested locally (19 tests), but they have
not yet run on a live instance. v6 log-reading endpoint is new in 0.6.0;
its tail/filter helpers are unit-tested locally, but it has not yet run on
a live instance.

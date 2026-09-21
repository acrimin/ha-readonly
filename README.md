# HA Readonly Inspector

An admin-only HTTP API for inspecting Home Assistant configuration — and, as
of v2, making guardrailed changes to automations. The repo name is historical:
v1 was strictly read-only; v2 adds writes that reuse the exact save path the
HA frontend itself uses.

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

## How writes stay safe

The write path is deliberately the same one the HA frontend uses, verified
against HA 2026.6.4 source (`homeassistant/components/config/view.py`,
`homeassistant/components/config/automation.py`):

1. **Same file.** UI-created automations are written to `automations.yaml`;
   this integration writes to that exact file. No `.storage` access.
2. **Same validation.** Every config is validated with HA's own
   `async_validate_config_item` before anything touches disk. Invalid configs
   are rejected with 400 and nothing is written.
3. **Same atomic write.** YAML is dumped before the file is opened and
   written atomically, under a mutation lock.
4. **Same reload.** After an applied create/update, only that one automation
   is reloaded via `automation.reload` with `{id: key}`. Deletes remove the
   entity-registry entry, exactly like the UI.

On top of that, this integration adds:

- **`dry_run` defaults to `true`.** Nothing is written, backed up, or
  reloaded unless the caller explicitly passes `"dry_run": false`.
- **Backup before every applied write.** `automations.yaml` is copied to a
  timestamped `.bak` file next to the original first.
- **Diffs.** Every response includes a unified diff of the automation's YAML
  (current vs proposed) for review before applying.
- **Audit logging.** Every write attempt is logged with user, key,
  applied/dry-run, and status. Request bodies are never logged.

Honest limits: validation is syntactic — a config can pass HA's schema and
still do the wrong thing (wrong light, runaway loop). The dry-run diff plus
explicit human approval is the mitigation; nothing in code can replace that
review. Scope is automations only; scripts and scenes remain read-only.

## Security model

- Every endpoint requires an authenticated **admin** user (normal HA auth).
  Non-admin requests get 403; unauthenticated get 401.
- v1 has no write path: no service calls, no file reads, no state changes.
- v2 write endpoints are admin-only, dry-run by default, validated, backed
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
tested on a live instance.

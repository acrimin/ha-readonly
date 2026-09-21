# HA Readonly Inspector

An admin-only, **read-only** HTTP API for inspecting Home Assistant configuration:
automations, scripts, scenes, registries, states, and repair issues. Built so an
agent can *see* your setup without any ability to change it.

## What it exposes (GET only)

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

## Security model

- Every endpoint requires an authenticated **admin** user (normal HA auth).
  Non-admin requests get 403; unauthenticated get 401.
- There is intentionally **no write path** anywhere in the code: no service
  calls, no file reads, no `.storage` access, no state changes.
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

Reviewed against HA 2026.6.4 and 2026.7.2 source. Not yet tested on a live
instance.

# Zigbee2MQTT Importer

Watches for Zigbee devices that have been re-paired out of Home Assistant's ZHA
integration into Zigbee2MQTT, and finishes the migration so the swap is
invisible: same area, stable entity ids, existing automations and scenes still
work, old ZHA device retired.

Devices are re-paired one at a time, by hand, at the device. This tool does
everything that comes after that.

## What it does

When a device appears in Zigbee2MQTT whose IEEE address still matches a live
ZHA device:

1. Renames the device in Zigbee2MQTT to the old ZHA name, temporarily, so
   anything auto-created by MQTT discovery in the meantime is recognisable.
2. Moves the old ZHA entity ids out of the way, prefixing them with
   `zz_migrated_`.
3. Disables the old ZHA device and renames it to `zz_migrated_<old name>`.
4. Gives the new device the old device's Home Assistant area.
5. Renames the new entities to stable ids derived from the canonical name.
6. Renames the device in Zigbee2MQTT to its canonical name.
7. Copies the old device's settings across to Zigbee2MQTT.
8. Registers every action the device can emit as a Home Assistant trigger.
9. Repoints every literal `entity_id` reference in `automations.yaml`,
   `scripts.yaml`, `scenes.yaml` and the Lovelace storage files at the new
   entity ids, backing every file up first.
10. Reloads the automation, script and scene domains, so no restart is needed.

**Dry run is the default.** Nothing is modified unless `--live` is passed.

### Why that order

The steps were specified as "retire the old device, then rename, then rewrite
references, then rename entity ids". Two things force a different order once
the entity registry is actually involved:

* The canonical name is normally derived from the old ZHA device name, so the
  entity ids it produces collide with the old device's existing entity ids.
  Home Assistant reserves the entity ids of disabled entities, so disabling the
  old device does not release them. The old ids have to be renamed away first.
* The config rewrite has to write the *final* entity ids. Rewriting to an
  intermediate id and renaming afterwards would leave every reference stale, so
  the rewrite happens last, immediately before the reload. That way the config
  is never reloaded while it points at an entity id that does not exist yet.

## Canonical naming

Names are `<Area> <Location> <Use>`, space separated: `Garage Overhead Middle`,
`Living Room Arch Sconces`, `kg Bathroom Sink Cans`.

The area always comes from the Home Assistant area the device is in, never from
the device name, because plenty of device names carry the wrong area or none at
all. Leading name tokens that merely repeat words from the area name are
dropped, so `Bathroom Closet Right` in area `kg Bathroom` becomes
`kg Bathroom Closet Right`.

The location and use have to come out of the free-form remainder of the ZHA
name, which is genuinely ambiguous. Exactly two remaining tokens is treated as
a clean split. Anything else gets a best-effort guess that is **logged for
review and not acted on**, because a wrong guess propagates into entity ids and
is tedious to undo.

Against the 96 devices currently eligible on this network: 43 derive cleanly,
30 produce a flagged guess, 23 cannot be derived at all (mostly devices with no
area set).

You do not have to write that file by hand. Whenever a name cannot be derived
with confidence, the entry is written to `workdir/name_overrides.json` for you,
prefilled with the best guess and keyed by normalised IEEE address:

```json
{
  "_help": "... how to confirm an entry ...",
  "0011223344556677": "TODO confirm: Stairway Ceiling Motion"
}
```

Correct the value if it is wrong, then delete the `TODO confirm: ` prefix. That
prefix is what keeps the entry inert: while it is there the name still counts
as unconfirmed and the device will not migrate, so the tool can never end up
acting on its own guess. A confirmed entry always wins over derivation and is
never treated as uncertain.

## Detecting a newly joined device

By polling the retained `zigbee2mqtt/bridge/devices` topic, not by subscribing
to `bridge/event`. The retained topic returns complete current state on every
poll, so nothing is missed while the tool is stopped or restarting, whereas
`device_joined` events are edge triggered and lost if they fire while the tool
is down. The trigger condition is a set intersection — a Z2M device whose IEEE
is still in the ZHA snapshot and not already recorded as migrated — which is
idempotent by construction, so polling adds no complexity.

## How Home Assistant is driven

Reads come straight from `.storage`: there is no REST endpoint for the device
or entity registries. The ZHA config entry id is looked up dynamically rather
than hardcoded.

Writes use whichever mechanism exists:

| Change | Mechanism |
| --- | --- |
| entity_id rename | REST `homeassistant.update_entity_id` (Spook) |
| device rename, disable, area | websocket `config/device_registry/update` |
| reloads | REST `automation.reload` and friends |

There is no REST service to rename a device, so that one goes over the
websocket API, run from inside the `homeassistant` container which bundles the
`websockets` library that the host virtualenv does not have.

Only the standard library is used for HTTP, so this tool adds no new dependency
to the shared virtualenv.

## Safety

* `workdir/` is git ignored. Every backup, snapshot, log and rollback script
  lives there and none of it is ever committed.
* Every file is copied to `workdir/backups/<run-id>_<filename>` before it is
  modified.
* Each migration writes `workdir/rollback_<run-id>_<ieee>.sh`, which restores
  the backed up files, reverts the entity id renames and the Z2M rename
  automatically, and then prints the two device registry changes that have no
  REST service and must be reverted by hand.
* Completed migrations are recorded in `workdir/completed_migrations.json` and
  never reprocessed. ZHA devices that are already disabled, or already named
  `zz_migrated_...` or `zz ...`, are excluded from the snapshot entirely.
* Anything that cannot be migrated automatically goes to
  `workdir/needs_review.log` as well as the main log. Nothing is skipped
  silently.

## Usage

Run via the shared supervisor in the parent directory:

```bash
./run.sh importer
```

Directly, for a single pass:

```bash
../venv/bin/python3 main.py --once            # dry run, safe, read only
sudo -E ../venv/bin/python3 main.py --once --live
```

`--live` requires root: the Home Assistant token and config files are not
readable or writable by an ordinary user.

| Option | Default | Meaning |
| --- | --- | --- |
| `--live` | off | Actually make changes |
| `--once` | off | Single pass, then exit |
| `--poll-interval` | 60 | Seconds between Z2M device list checks |
| `--snapshot-refresh-hours` | 48 | How stale the ZHA snapshot may get |
| `--workdir` | `./workdir` | Where state, backups and logs live |

MQTT connection details come from `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER` and
`MQTT_PASSWORD`, which `run.sh` exports.

## Modules

| File | Purpose |
| --- | --- |
| `main.py` | Argument parsing, the watcher loop, and the migration sequence |
| `zha.py` | The periodic ZHA device snapshot and IEEE normalisation |
| `ha.py` | Home Assistant registry reads and live registry mutations |
| `ha_ws_call.py` | Websocket helper, copied into the HA container to run |
| `z2m.py` | Zigbee2MQTT bridge queries and device renames |
| `rewrite.py` | Token-aware entity_id reference rewriting, with backups |
| `settings.py` | ZHA to Z2M settings map and value translation |
| `triggers.py` | Home Assistant action trigger discovery configs |
| `naming.py` | Canonical `<Area> <Location> <Use>` derivation |

`naming.py`, `rewrite.py`, `settings.py` and `triggers.py` all run their own self-checks when executed
directly, which is where the logic worth breaking lives.

## Action triggers

Zigbee2MQTT only publishes the Home Assistant `device_automation` discovery
config for an action the first time that exact value is actually seen coming
off the device. It never publishes the static `exposes.action.values` enum up
front. So until somebody physically performs every gesture, most of a switch's
triggers are not selectable in Home Assistant at all: a VZM31-SN advertises 21
action values and on this network they typically have about five.

Migration is the right moment to fix that, so every advertised action value
that has no discovery config gets one published directly. Values that already
have a config are left alone, which makes this idempotent and avoids replacing
a real config with a reconstructed one.

**Only the discovery config topic is ever published.** The device's own
`zigbee2mqtt/<name>/action` topic is never touched: that one carries real
events, and anything already subscribed to it would fire for real. The payload
contract was taken from the Zigbee2MQTT source and is asserted in
`triggers.py`'s self-check by rebuilding a real retained config byte for byte.

This is not model specific: it applies to anything with an `action` expose,
including multi-button remotes and motion sensors.

## Settings migration

A re-paired device comes up on Zigbee2MQTT with factory defaults, so its
settings are copied across from ZHA as part of the migration. Only the Inovelli
VZM31-SN is mapped, that being the bulk of this migration; any other model is
reported as having no settings map rather than quietly skipped.

The map lives in `settings.py`, keyed by the ZHA **attribute** name taken from
the tail of the entity's `unique_id`. It is deliberately not keyed by
entity_id, because on a real VZM31-SN the entity ids cannot be trusted: three
of them are literally called `..._none`, and the entity whose id ends
`_button_delay` is actually the local dimming up speed, mislabelled by a ZHA
quirk. The unique_id tail is the attribute the device itself reports.

Values are not blindly copied. Each entry says how to convert:

| Rule | Meaning |
| --- | --- |
| `number` | copy across as an integer |
| `text` | copy across as a string, the wording already matches |
| `index` | the ZHA number is an index into the Z2M enum's values |
| `{...}` | an explicit ZHA value to Z2M value translation |

The `index` and translation rules matter more than they look. Z2M's
`buttonDelay` is the enum `0ms`..`900ms`, so ZHA's `5` means `500ms`, not `5`.
`onOffLedMode` is `All`/`One`, so a raw boolean copy is rejected outright. And
both `relayClick` and `doubleTapClearNotifications` are phrased as "disable X"
in ZHA while Z2M names the parameter rather than the effect, so the polarity
has to be read off the actual Z2M value text.

Every planned write is checked against the target device's own `exposes`
definition before it is sent, so an out of range number, an enum value that
does not exist, or a read-only attribute is reported rather than published and
silently dropped. Anything with no mapping is reported with the reason.

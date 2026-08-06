#!/usr/bin/env python3
"""Watch for devices re-paired from ZHA into Zigbee2MQTT and finish the move.

Chris re-pairs one physical device at a time. When a device turns up in
Zigbee2MQTT whose IEEE address still matches a live ZHA device, this tool makes
the switch transparent: it retires the old ZHA device, gives the new one the
old device's area, renames the new entities to stable ids derived from the
canonical `<Area> <Location> <Use>` name, repoints every literal entity_id
reference in the Home Assistant config at the new ids, and renames the device
in Zigbee2MQTT.

Dry run is the default. Nothing is ever modified unless `--live` is passed.
"""

import argparse
import json
import logging
import os
import shlex
import sys
import time

import ha
import naming
import rewrite
import settings
import triggers
import z2m
import zha

MIGRATED_PREFIX = "zz_migrated_"
# Entity domains that carry device configuration rather than telemetry.
SETTINGS_DOMAINS = ("number", "select", "switch")
# Marks the throwaway name a device wears between its old Zigbee2MQTT name and
# its canonical one, so that name cannot collide with one another device
# already holds.
INTERMEDIATE_SUFFIX = "__"
RELOAD_DOMAINS = ("automation", "script", "scene")

logger = logging.getLogger("importer")
review = logging.getLogger("importer.review")


def setup_logging(workdir):
    """Log everything to stdout and a file, and manual work to its own file."""
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    for handler in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(workdir, "importer.log")),
    ):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Anything a human has to deal with is duplicated into its own log so it is
    # never lost in the general chatter.
    review_handler = logging.FileHandler(os.path.join(workdir, "needs_review.log"))
    review_handler.setFormatter(formatter)
    review.addHandler(review_handler)
    review.setLevel(logging.WARNING)
    review.propagate = False  # needs_review() already logs it to the main log


def needs_review(message, *args):
    """Record something that could not be migrated automatically."""
    logger.warning("NEEDS REVIEW: " + message, *args)
    review.warning(message, *args)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as handle:
        return json.load(handle)


def save_json(path, payload):
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def entity_key(entity):
    """Pair entities across integrations by domain and the name of the trait.

    ZHA and Z2M both expose e.g. an `Occupancy` binary_sensor and a
    `Temperature` sensor, and the primary entity of a light or switch has no
    original_name on either side, so this pairs the two devices' entities
    without hardcoding any device specific knowledge.
    """
    return entity["entity_id"].split(".", 1)[0], (entity.get("original_name") or "").strip().lower()


def enabled(entities):
    return [entity for entity in entities if entity.get("disabled_by") is None]


def pair_entities(old_entities, new_entities):
    """Return (pairs, unpaired_old) matching old ZHA entities to new Z2M ones."""
    by_key = {}
    for entity in new_entities:
        by_key.setdefault(entity_key(entity), []).append(entity)

    pairs, unpaired = [], []
    for old in old_entities:
        candidates = by_key.get(entity_key(old), [])
        if len(candidates) == 1:
            pairs.append((old, candidates[0]))
        else:
            unpaired.append((old, len(candidates)))
    return pairs, unpaired


def final_entity_id(canonical, entity):
    """The stable entity id a new entity should end up with."""
    domain = entity["entity_id"].split(".", 1)[0]
    suffix = naming.slug(entity.get("original_name") or "")
    base = naming.slug(canonical)
    return f"{domain}.{base}_{suffix}" if suffix else f"{domain}.{base}"


def retired_entity_id(entity):
    """Where an old ZHA entity id is moved to, to free up the namespace."""
    domain, object_id = entity["entity_id"].split(".", 1)
    return f"{domain}.{MIGRATED_PREFIX}{object_id}"


def last_recorded_value(entries):
    """The most recent recorded state that was an actual value."""
    for entry in reversed(entries):
        state = entry.get("state")
        if state is not None and str(state).strip().lower() not in settings.NO_VALUE:
            return state
    return None


def plan_settings(old_entities, z2m_device, history_days):
    """Work out which of the old device's settings to copy into Zigbee2MQTT.

    Values come from the recorder, not from the entities' current state. By the
    time a device shows up in Zigbee2MQTT it has already left the ZHA network,
    so ZHA cannot poll it any more and every one of its entities reads
    unavailable. Reading live state here could never have worked for any
    device. The recorder still holds the last real value each entity had, from
    the instant before it went unavailable, which is exactly what needs
    copying.
    """
    # Only configuration entities carry settings; everything else is telemetry.
    config_entities = [
        entity for entity in old_entities if entity["entity_id"].split(".", 1)[0] in SETTINGS_DOMAINS
    ]
    if not config_entities:
        return {}, []

    try:
        recorded = ha.history([entity["entity_id"] for entity in config_entities], history_days)
    except OSError as error:
        return {}, [f"cannot read recorder history ({error}), settings were not migrated"]

    zha_values, notes = {}, []
    for entity in config_entities:
        value = last_recorded_value(recorded.get(entity["entity_id"], []))
        if value is None:
            notes.append(
                f"{settings.zha_attribute(entity)} has no real value in the last "
                f"{history_days} days of recorder history ({entity['entity_id']}), not migrated"
            )
            continue
        zha_values[settings.zha_attribute(entity)] = value

    model = (z2m_device.get("definition") or {}).get("model", "unknown")
    writes, plan_notes = settings.plan(model, zha_values, z2m.exposes(z2m_device))
    return writes, notes + plan_notes


def plan_triggers(ieee, z2m_device, canonical):
    """Work out which action trigger discovery configs are missing or stale.

    These are published after the canonical rename, so they are built for the
    name the device will have by then, not the one it still has here.
    """
    bridge = z2m.info()
    coordinator = next(
        (device["ieee_address"] for device in z2m.devices() if device.get("type") == "Coordinator"),
        None,
    )
    if coordinator is None:
        needs_review("%s: no coordinator in the bridge device list, cannot build trigger configs", ieee)
        return []

    address = z2m_device["ieee_address"]
    retained = z2m.collect(f"{triggers.DISCOVERY_PREFIX}/device_automation/{address}/+/config")
    return triggers.plan(
        z2m_device,
        z2m.exposes(z2m_device),
        retained,
        coordinator,
        bridge.get("version", "unknown"),
        bridge["config"]["mqtt"]["base_topic"],
        canonical,
    )


def build_plan(ieee, zha_info, z2m_device, overrides_path, history_days):
    """Work out everything that would change, without changing anything."""
    z2m_name = z2m_device["friendly_name"]
    canonical, uncertainty = naming.canonical_for(
        ieee, zha_info["name"], zha_info["area_name"], naming.load_overrides(overrides_path)
    )
    if uncertainty or canonical is None:
        # Write the entry out prefilled, so confirming it is an edit rather
        # than hand-writing JSON. The prefix keeps it inert until then.
        proposed = canonical or f"{zha_info['area_name'] or '<Area>'} <Location> <Use>"
        added = naming.write_template(overrides_path, ieee, proposed, zha_name=zha_info["name"])
        needs_review(
            "%s (ZHA %r, Z2M %r): %s. %s %s as %r, correct it if needed and remove the "
            "%r prefix to confirm, then it will migrate.",
            ieee,
            zha_info["name"],
            z2m_name,
            uncertainty,
            "wrote it to" if added else "an entry is already waiting in",
            overrides_path,
            proposed,
            naming.TEMPLATE_PREFIX,
        )
        return None

    all_devices, all_entities = ha.devices(), ha.entities()
    new_device = ha.mqtt_device_by_ieee(all_devices, ieee)
    if new_device is None:
        needs_review(
            "%s (Z2M %r): no MQTT device registry entry yet, discovery may still be in flight",
            ieee,
            z2m_name,
        )
        return None

    all_old_entities = ha.device_entities(all_entities, zha_info["ha_device_id"])
    settings_writes, settings_notes = plan_settings(all_old_entities, z2m_device, history_days)
    trigger_writes = plan_triggers(ieee, z2m_device, canonical)
    for note in settings_notes:
        needs_review("%s settings: %s", ieee, note)

    old_entities = enabled(all_old_entities)
    new_entities = enabled(ha.device_entities(all_entities, new_device["id"]))
    pairs, unpaired = pair_entities(old_entities, new_entities)

    # An unpaired old entity is only a problem if something actually refers to
    # it, so say where, rather than making Chris go and look.
    unpaired_references = rewrite.find_references(
        rewrite.target_files(ha.CONFIG_DIR), [old["entity_id"] for old, _ in unpaired]
    )
    for old, candidate_count in unpaired:
        used_by = [
            f"{os.path.basename(hit['path'])} x{hit['count']}"
            for hit in unpaired_references
            if hit["entity_id"] == old["entity_id"]
        ]
        # Only worth naming a likely counterpart when something actually
        # refers to the old entity, otherwise it is noise on a dead entity.
        suggestion = ""
        if used_by:
            close = naming.closest_entity_id(old["entity_id"], [new["entity_id"] for new in new_entities])
            if close:
                suggestion = f"; {close[0]} looks related, confirm and apply by hand"

        needs_review(
            "%s: old ZHA entity %s has %s matching Z2M entity, references to it cannot "
            "be repointed automatically (%s)%s",
            ieee,
            old["entity_id"],
            "no" if candidate_count == 0 else f"{candidate_count} ambiguous",
            "referenced in " + ", ".join(used_by) if used_by else "not referenced anywhere, safe to ignore",
            suggestion,
        )

    taken = {entity["entity_id"] for entity in all_entities}
    retiring = {old["entity_id"] for old, _ in pairs}

    renames, reference_map = [], {}
    for old, new in pairs:
        target = final_entity_id(canonical, new)
        if target in taken and target not in retiring and target != new["entity_id"]:
            needs_review(
                "%s: wanted to rename %s -> %s but that entity id is already taken by "
                "something unrelated, leaving it alone",
                ieee,
                new["entity_id"],
                target,
            )
            continue
        renames.append((old, new, target))
        reference_map[old["entity_id"]] = target

    # A migration is not a rename. The Zigbee2MQTT device is a brand new device
    # registry entry under a different integration, so it gets a different
    # device_id, and every registry reference to the old one breaks exactly the
    # way a stale entity_id does. Registry references survive an ordinary
    # in-place rename, which is why they were originally left alone, but they
    # do not survive this. Token replacement handles them without caring which
    # key they sit under, which matters because blueprint inputs name their own
    # keys (`light_target`, `switch_target`, anything) rather than following a
    # fixed schema.
    reference_map[zha_info["ha_device_id"]] = new_device["id"]

    # The intermediate name carries a marker suffix so it cannot collide with a
    # name something else already has. Renaming straight to the old ZHA name
    # fails outright when another device in Zigbee2MQTT already goes by it,
    # which is common when the old ZHA names were never unique to begin with.
    #
    # This never collapses onto canonical even when z2m_name already equals
    # it. That collapse used to be here as an optimisation ("nothing to
    # rename"), but z2m.rename()'s own old_name==new_name guard means a
    # collapsed intermediate silently skips the LATER canonical rename too -
    # and that later rename is the one carrying homeassistant_rename=True,
    # which is what makes Home Assistant recreate every entity under a clean
    # id. Skipping it left every entity that never paired with an old ZHA
    # entity (i.e. most of them - anything ZHA never exposed) stuck on its
    # raw `0x<ieee>_*` MQTT-discovery id forever, even though the device's
    # own Z2M name looked perfectly correct. Always using a genuinely
    # different intermediate guarantees the canonical rename is always a
    # real string change, so it always fires.
    intermediate = zha_info["name"] + INTERMEDIATE_SUFFIX

    return {
        "ieee": ieee,
        "canonical": canonical,
        "intermediate_name": intermediate,
        "zha": zha_info,
        "z2m_name": z2m_name,
        "new_device_id": new_device["id"],
        "renames": renames,
        "reference_map": reference_map,
        "settings_writes": settings_writes,
        "trigger_writes": trigger_writes,
    }


def write_rollback(path, plan, run_id, restored):
    """Emit a rollback script for this run.

    File restores, entity id renames and the Z2M rename all revert cleanly and
    are done automatically. The device registry disable and rename have no REST
    service, so those are printed as explicit manual steps rather than pretended
    to be automatic.
    """
    lines = [
        "#!/bin/bash",
        "set -Eeuo pipefail",
        "",
        f"# Rollback for migration run {run_id}",
        f"# Device: {plan['ieee']}  ZHA {plan['zha']['name']!r} -> Z2M {plan['canonical']!r}",
        "",
        'TOKEN="$(sudo cat /root/.ha_token_claude | tr -d "\\n")"',
        "",
        "ha_call() {",
        '  curl -s -f -X POST -H "Authorization: Bearer ${TOKEN}" '
        '-H "Content-Type: application/json" -d "${2}" '
        '"http://127.0.0.1:8123/api/services/${1}" > /dev/null',
        "}",
        "",
        "echo 'Restoring backed up files...'",
    ]
    for record in restored:
        lines.append(f"sudo cp -p {shlex.quote(record['backup'])} {shlex.quote(record['path'])}")

    lines += ["", "echo 'Reverting entity id renames...'"]
    for old, new, target in plan["renames"]:
        lines.append(
            "ha_call homeassistant/update_entity_id "
            + shlex.quote(json.dumps({"entity_id": target, "new_entity_id": new["entity_id"]}))
        )
        lines.append(
            "ha_call homeassistant/update_entity_id "
            + shlex.quote(
                json.dumps({"entity_id": retired_entity_id(old), "new_entity_id": old["entity_id"]})
            )
        )

    rename_payload = json.dumps({"from": plan["canonical"], "to": plan["z2m_name"]})
    lines += [
        "",
        "echo 'Reverting the Zigbee2MQTT rename...'",
        "docker exec mosquitto mosquitto_pub -h localhost -p 1884 -u z2m -P z2m "
        f"-t zigbee2mqtt/bridge/request/device/rename -m {shlex.quote(rename_payload)}",
        "",
        "for domain in " + " ".join(RELOAD_DOMAINS) + "; do ha_call \"${domain}/reload\" '{}'; done",
        "",
        "cat <<'MANUAL'",
        "",
        "Done, except for two device registry changes that Home Assistant exposes no",
        "REST service for. Revert these by hand in Settings -> Devices:",
        "",
        f"  Device: {plan['zha']['name']}  (registry id {plan['zha']['ha_device_id']})",
        f"    1. Re-enable it (it was disabled by this migration).",
        f"    2. Rename it from '{MIGRATED_PREFIX}{plan['zha']['name']}' back to '{plan['zha']['name']}'.",
        "",
        "MANUAL",
    ]

    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)


def execute(plan, workdir, run_id, dry_run):
    """Carry out a migration plan.

    The step order differs from the order the steps were specified in, for two
    reasons that only show up once the entity registry is involved:

    * the old ZHA entity ids have to be moved out of the way first, because the
      canonical name is usually derived from the old device name and so the new
      entity ids collide with the old ones,
    * the config file rewrite happens last and points straight at the final
      entity ids, so the config is never reloaded while it references an entity
      id that does not exist yet.
    """
    prefix = "[DRY RUN] would " if dry_run else ""
    zha_info = plan["zha"]
    address = f"0x{plan['ieee']}"
    logger.info(
        "%smigrate %s: ZHA %r -> Z2M %r (currently %r)",
        prefix,
        plan["ieee"],
        zha_info["name"],
        plan["canonical"],
        plan["z2m_name"],
    )

    # (b) Temporary Z2M name, so anything auto-created by MQTT discovery in the
    # meantime shows up under a recognisable name.
    if plan["z2m_name"] == plan["intermediate_name"]:
        logger.info("Z2M name is already %r, no temporary rename needed", plan["z2m_name"])
    else:
        logger.info(
            "%srename in Z2M: %r -> %r (temporary)", prefix, plan["z2m_name"], plan["intermediate_name"]
        )
        if not dry_run:
            z2m.rename(plan["z2m_name"], plan["intermediate_name"], ieee_address=address)

    # Free the entity id namespace before claiming it for the new entities.
    for old, _new, _target in plan["renames"]:
        if old["entity_id"] == retired_entity_id(old):
            continue
        logger.info("%sretire old entity id: %s -> %s", prefix, old["entity_id"], retired_entity_id(old))
        if not dry_run:
            ha.update_entity_id(old["entity_id"], retired_entity_id(old))

    # (a) Retire the old ZHA device.
    logger.info(
        "%sdisable ZHA device %s and rename it to %r",
        prefix,
        zha_info["ha_device_id"],
        MIGRATED_PREFIX + zha_info["name"],
    )
    if not dry_run:
        ha.update_device(
            zha_info["ha_device_id"],
            name_by_user=MIGRATED_PREFIX + zha_info["name"],
            disabled_by="user",
        )

    # (c) Give the new device the old device's area.
    logger.info("%sset new device area to %r (%s)", prefix, zha_info["area_name"], zha_info["area_id"])
    if not dry_run:
        ha.update_device(plan["new_device_id"], area_id=zha_info["area_id"])

    # (e, f) Final canonical Z2M name, which also gives the device its entity
    # ids. Renaming with `homeassistant_rename` republishes the discovery
    # topics, so Home Assistant recreates every one of the device's entities
    # named after the canonical name. Renaming entities individually only ever
    # reached the ones that paired with an old ZHA entity, and left the rest
    # stuck with the raw `0x<ieee>` ids MQTT discovery first gave them.
    for _old, _new, target in plan["renames"]:
        logger.info("%sexpect entity id %s", prefix, target)

    # intermediate_name always carries INTERMEDIATE_SUFFIX now (see
    # build_plan), so it can never equal canonical - this rename always fires
    # and always carries homeassistant_rename=True.
    logger.info(
        "%srename in Z2M: %r -> %r (canonical, recreating entities)",
        prefix,
        plan["intermediate_name"],
        plan["canonical"],
    )
    if not dry_run:
        z2m.rename(
            plan["intermediate_name"],
            plan["canonical"],
            ieee_address=address,
            homeassistant_rename=True,
        )
        # Home Assistant recreates the entities asynchronously, and the
        # config rewrite below points at the ids predicted here, so make
        # sure they actually turned up rather than assuming it.
        time.sleep(10)
        live = {entity["entity_id"] for entity in ha.entities()}
        for _old, _new, target in plan["renames"]:
            if target not in live:
                needs_review(
                    "%s: expected entity %s after the canonical rename but it does not exist; "
                    "references repointed at it will be dangling",
                    plan["ieee"],
                    target,
                )

    # (g) Copy the old device's settings across.
    for attribute, value in sorted(plan["settings_writes"].items()):
        logger.info("%sset %s = %r", prefix, attribute, value)
    if plan["settings_writes"] and not dry_run:
        z2m.set_attributes(plan["canonical"], plan["settings_writes"])
    if not plan["settings_writes"]:
        logger.info("no settings to copy across")

    # Register every action the device can emit as a Home Assistant trigger,
    # so they are all selectable without having to perform each gesture first,
    # and repoint any that Zigbee2MQTT left on the pre-rename name. This has
    # to run after the canonical rename, and only ever writes discovery config
    # topics, never the action topic.
    for topic, _payload in plan["trigger_writes"]:
        logger.info("%sregister trigger %s", prefix, topic.rsplit("/", 2)[-2])
    if plan["trigger_writes"]:
        logger.info("%spublish %s trigger discovery config(s)", prefix, len(plan["trigger_writes"]))
        if not dry_run:
            z2m.publish_retained(
                [(topic, triggers.encode(payload)) for topic, payload in plan["trigger_writes"]]
            )
    else:
        logger.info("no missing action triggers to register")

    # (d) Repoint literal entity_id references, backing every file up first.
    backup_dir = os.path.join(workdir, "backups")
    changed = rewrite.rewrite_files(ha.CONFIG_DIR, plan["reference_map"], backup_dir, run_id, dry_run)
    for record in changed:
        logger.info(
            "%srewrite %s (%s)",
            prefix,
            record["path"],
            ", ".join(f"{old} x{count}" for old, count in record["counts"].items()),
        )
    if not changed:
        logger.info("no literal entity_id references needed rewriting")

    for stray in rewrite.stray_references(
        ha.CONFIG_DIR, list(plan["reference_map"]), [record["path"] for record in changed]
    ):
        needs_review(
            "%s: %s still references %s (%s time(s)) and is not a file this tool rewrites, fix it by hand",
            plan["ieee"],
            stray["path"],
            stray["entity_id"],
            stray["count"],
        )

    if dry_run:
        logger.info("[DRY RUN] would reload: %s", ", ".join(RELOAD_DOMAINS))
        logger.info("[DRY RUN] no rollback script written, nothing was changed")
        return

    ha.reload_domains(RELOAD_DOMAINS)

    rollback_path = os.path.join(workdir, f"rollback_{run_id}_{plan['ieee']}.sh")
    write_rollback(rollback_path, plan, run_id, changed)
    logger.info("rollback script written to %s", rollback_path)


def run_once(args, workdir):
    """One pass: refresh the snapshot if due, then migrate anything matching."""
    snapshot_path = os.path.join(workdir, "zha_snapshot.json")
    snapshot, refreshed = zha.load_snapshot(snapshot_path, args.snapshot_refresh_hours)
    if refreshed:
        logger.info("refreshed ZHA snapshot: %s devices eligible for migration", len(snapshot))

    completed_path = os.path.join(workdir, "completed_migrations.json")
    completed = load_json(completed_path, {})
    overrides_path = os.path.join(workdir, "name_overrides.json")

    paired = z2m.paired_devices()
    candidates = sorted(set(paired) & set(snapshot) - set(completed))
    if not candidates:
        logger.info("no devices to migrate (%s in Z2M, %s eligible in ZHA)", len(paired), len(snapshot))
        return

    logger.info("%s device(s) present in both Z2M and the ZHA snapshot", len(candidates))
    for ieee in candidates:
        run_id = time.strftime("%Y%m%d-%H%M%S")
        plan = build_plan(ieee, snapshot[ieee], paired[ieee], overrides_path, args.history_days)
        if plan is None:
            continue

        try:
            execute(plan, workdir, run_id, args.dry_run)
        except Exception as error:  # keep the watcher alive, but never silently
            needs_review("%s: migration failed part way through: %s", ieee, error)
            continue

        if not args.dry_run:
            completed[ieee] = {
                "run_id": run_id,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "zha_name": snapshot[ieee]["name"],
                "canonical": plan["canonical"],
                # Recorded so a later audit can check the old device_id really
                # did stop being referenced, without re-deriving the pair from
                # the registry.
                "old_device_id": snapshot[ieee]["ha_device_id"],
                "new_device_id": plan["new_device_id"],
            }
            save_json(completed_path, completed)
            logger.info("migration of %s complete", ieee)


def parse_args():
    parser = argparse.ArgumentParser(description="Migrate ZHA devices to Zigbee2MQTT as they are re-paired")
    parser.add_argument(
        "--live",
        dest="dry_run",
        action="store_false",
        default=True,
        help="Actually make changes. Without this the tool only reports what it would do.",
    )
    parser.add_argument(
        "--snapshot-refresh-hours",
        type=float,
        default=48.0,
        help="How old the ZHA snapshot may get before it is rebuilt (default: 48)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=60,
        help="Seconds between checks of the Z2M device list (default: 60)",
    )
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument(
        "--history-days",
        type=float,
        default=7.0,
        help="How far back to look in the recorder for a device's last real settings "
        "values (default: 7, and Home Assistant keeps 10 by default)",
    )
    parser.add_argument(
        "--workdir",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "workdir"),
        help="Where snapshots, backups, logs and rollback scripts live",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    workdir = args.workdir
    os.makedirs(workdir, exist_ok=True)
    setup_logging(workdir)

    if args.dry_run:
        logger.info("DRY RUN: nothing will be modified. Pass --live to make changes.")
    elif os.geteuid() != 0:
        logger.error(
            "--live needs root: the token and the Home Assistant config files are not "
            "writable by this user. Re-run with `sudo -E`."
        )
        return 1

    while True:
        try:
            run_once(args, workdir)
        except Exception as error:
            logger.exception("pass failed: %s", error)
        if args.once:
            return 0
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())

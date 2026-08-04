#!/usr/bin/env python3
"""Rewrite literal entity_id references across Home Assistant's config files.

Only literal string references need this. Device actions that reference the
registry by uuid (`device_id: <uuid>` plus `entity_id: <uuid>`), which is the
dominant style in this house's `automations.yaml`, are registry stable and
survive an entity_id rename untouched, so they are left alone.

The rewrite is a token-aware regex, not a substring replace: replacing
`switch.old_name` naively would also corrupt `switch.old_name_2`. Files are
edited in place so that root-owned files keep their owner and mode, and every
file is copied to the backup directory before it is touched.
"""

import glob
import os
import re
import shutil

# Entity ids are made of these characters, so a real reference is never
# directly adjacent to one of them.
_BOUNDARY = r"[A-Za-z0-9_.]"


def target_files(config_dir):
    """The config and storage files that can hold literal entity ids."""
    files = [os.path.join(config_dir, name) for name in ("automations.yaml", "scripts.yaml", "scenes.yaml")]
    files += sorted(glob.glob(os.path.join(config_dir, ".storage", "lovelace*")))
    return [path for path in files if os.path.isfile(path)]


def _pattern(entity_id):
    return re.compile(f"(?<!{_BOUNDARY}){re.escape(entity_id)}(?!{_BOUNDARY})")


def count_references(text, entity_id):
    """How many whole-token references to entity_id the text contains."""
    return len(_pattern(entity_id).findall(text))


def apply_mapping(text, mapping):
    """Replace every whole-token entity id in mapping, returning (text, counts)."""
    counts = {}
    for old_id, new_id in mapping.items():
        pattern = _pattern(old_id)
        text, replaced = pattern.subn(new_id, text)
        if replaced:
            counts[old_id] = replaced
    return text, counts


def backup(path, backup_dir, run_stamp):
    """Copy a file to the backup directory before it is modified."""
    os.makedirs(backup_dir, exist_ok=True)
    destination = os.path.join(backup_dir, f"{run_stamp}_{os.path.basename(path)}")
    shutil.copy2(path, destination)
    return destination


def rewrite_files(config_dir, mapping, backup_dir, run_stamp, dry_run):
    """Rewrite every target file, backing each one up first.

    Returns a list of {path, backup, counts} for the files that changed.
    """
    changed = []
    for path in target_files(config_dir):
        with open(path) as handle:
            original = handle.read()

        updated, counts = apply_mapping(original, mapping)
        if not counts:
            continue

        record = {"path": path, "counts": counts, "backup": None}
        if not dry_run:
            record["backup"] = backup(path, backup_dir, run_stamp)
            with open(path, "w") as handle:  # in place, to preserve owner and mode
                handle.write(updated)
        changed.append(record)
    return changed


def find_references(paths, entity_ids):
    """Count whole-token references to each entity id across the given files."""
    found = []
    for path in paths:
        try:
            with open(path, errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for entity_id in entity_ids:
            hits = count_references(text, entity_id)
            if hits:
                found.append({"path": path, "entity_id": entity_id, "count": hits})
    return found


def _scannable_files(config_dir, skip):
    """Config files worth scanning for references this tool will not rewrite.

    Home Assistant's own `core.*` registries are excluded: they hold the entity
    ids by definition, and Home Assistant rewrites them itself in response to
    the rename, so reporting them would be pure noise.
    """
    for root, dirs, names in os.walk(config_dir):
        dirs[:] = [d for d in dirs if d not in ("deps", "tts", "www", "custom_components", "backups")]
        in_storage = os.path.basename(root) == ".storage"
        for name in names:
            path = os.path.join(root, name)
            if path in skip or (in_storage and name.startswith("core.")):
                continue
            if in_storage or name.endswith((".yaml", ".yml", ".json")):
                yield path


def stray_references(config_dir, entity_ids, rewritten_paths):
    """Find references left in files this tool does not rewrite.

    Anything reported here breaks when the old entity id goes away, and has to
    be fixed by hand, so it belongs in the manual review log.
    """
    skip = set(rewritten_paths) | set(target_files(config_dir))
    return find_references(_scannable_files(config_dir, skip), entity_ids)


def _self_check():
    text = (
        "entity_id: switch.old_name\n"
        "entity_id: switch.old_name_2\n"
        "value: \"{{ states('switch.old_name') }}\"\n"
        "other: sensor.switch.old_name\n"
    )
    assert count_references(text, "switch.old_name") == 2, count_references(text, "switch.old_name")

    updated, counts = apply_mapping(text, {"switch.old_name": "switch.new_name"})
    assert counts == {"switch.old_name": 2}
    assert "switch.old_name_2" in updated, "must not corrupt a longer sibling entity id"
    assert "sensor.switch.old_name" in updated, "must not match a dotted suffix"
    assert updated.count("switch.new_name") == 2

    # Registry device_id references break on a migration too, because the new
    # device is a new registry entry rather than a renamed one. Blueprint inputs
    # name their own keys, so this has to work whatever key the device_id sits
    # under. This is the exact shape that silently broke a live
    # motion-activated light automation.
    old_device, new_device = "e70954ddd95087226cb3a0d7b6ca18cd", "2c28c158537319bda7163c67da7bb697"
    blueprint = (
        "use_blueprint:\n"
        "  path: homeassistant/motion_light.yaml\n"
        "  input:\n"
        "    motion_entity: binary_sensor.closet_motion_sensor_motion\n"
        f"    light_target:\n      device_id: {old_device}\n"
        f"target:\n  device_id: {old_device}\n"
    )
    rewritten, device_counts = apply_mapping(blueprint, {old_device: new_device})
    assert device_counts == {old_device: 2}, device_counts
    assert old_device not in rewritten
    assert rewritten.count(new_device) == 2

    print("rewrite self-check OK")


if __name__ == "__main__":
    _self_check()

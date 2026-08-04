#!/usr/bin/env python3
"""Snapshot the ZHA devices that are still candidates for migration.

Reads Home Assistant's own storage registries directly (they are world
readable) rather than the REST API, because the device registry is not exposed
over REST at all. The ZHA config entry id is looked up dynamically instead of
being hardcoded, so this keeps working if the coordinator is ever re-added.
"""

import json
import os
import time

import ha

# Devices whose resolved name starts with one of these are already migrated:
# `zz_migrated_` is this tool's own prefix, `zz ` covers the hand-migrations
# that predate it (e.g. "zz (done) Top of Stairs Right Dummy").
MIGRATED_NAME_PREFIXES = ("zz_", "zz ")


def norm_ieee(value):
    """Normalise an IEEE address for comparison across ZHA and Z2M.

    ZHA stores `aa:bb:cc:dd:ee:ff:00:11`, Z2M stores `0xaabbccddeeff0011`.
    """
    return value.replace("0x", "").replace(":", "").lower()


def device_name(device):
    """The name Chris sees in Home Assistant for a device registry entry."""
    return (device.get("name_by_user") or device.get("name") or "").strip()


def is_already_migrated(device):
    """True if this ZHA device has already been dealt with, by us or by hand."""
    if device.get("disabled_by") is not None:
        return True
    return device_name(device).lower().startswith(MIGRATED_NAME_PREFIXES)


def zha_devices():
    """Every device registry entry belonging to the ZHA config entry."""
    entry_id = ha.zha_entry_id()
    return [device for device in ha.devices() if entry_id in (device.get("config_entries") or [])]


def device_ieee(device):
    """The ZHA IEEE address of a device registry entry, normalised."""
    for identifier in device.get("identifiers") or []:
        if len(identifier) > 1 and identifier[0] == "zha":
            return norm_ieee(str(identifier[1]))
    return None


def build_snapshot():
    """Map normalised IEEE -> {name, area_id, area_name, ha_device_id}."""
    areas = ha.areas()
    snapshot = {}
    for device in zha_devices():
        if is_already_migrated(device):
            continue
        ieee = device_ieee(device)
        if not ieee:
            continue
        area_id = device.get("area_id")
        snapshot[ieee] = {
            "name": device_name(device),
            "area_id": area_id,
            "area_name": areas.get(area_id, "") if area_id else "",
            "ha_device_id": device["id"],
        }
    return snapshot


def write_snapshot(path):
    """Rebuild the snapshot and persist it, returning the snapshot itself."""
    snapshot = build_snapshot()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump({"generated_at": time.time(), "devices": snapshot}, handle, indent=2, sort_keys=True)
    return snapshot


def load_snapshot(path, max_age_hours):
    """Return the persisted snapshot, rebuilding it if missing or stale."""
    if os.path.exists(path):
        with open(path) as handle:
            stored = json.load(handle)
        age_hours = (time.time() - stored.get("generated_at", 0)) / 3600
        if age_hours < max_age_hours:
            return stored["devices"], False
    return write_snapshot(path), True


if __name__ == "__main__":
    import sys

    devices = build_snapshot()
    print(f"{len(devices)} ZHA devices eligible for migration", file=sys.stderr)
    print(json.dumps(devices, indent=2, sort_keys=True))

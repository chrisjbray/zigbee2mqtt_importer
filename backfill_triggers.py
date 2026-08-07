#!/usr/bin/env python3
"""Publish the device trigger configs Zigbee2MQTT never got round to writing.

Zigbee2MQTT only publishes a `device_automation` discovery config the first
time an action value is actually seen coming off the device, so until somebody
has physically performed every gesture most of a switch's triggers are not
selectable in Home Assistant at all. `triggers.plan()` builds the full set from
the device's own `exposes`.

The importer already does this as part of a migration. This applies the same
thing to devices that are already migrated - a device paired outside the tool,
or one whose triggers were only ever half-populated.

**Only discovery config topics are ever published.** The device's own action
topic is never touched: that one carries real events, and anything subscribed
to it would fire for real.

Dry run by default; add --live to publish.

Usage
-----

`z2m.py` reads the broker from `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER` and
`MQTT_PASSWORD`, and `run.sh` has no wrapper for this one, so set them the same
way its `creds()` function does. Take the values from there rather than copying
them into anything tracked::

    cd /data/config/z2m/tools
    set -a; eval "$(sed -n '/^creds() {/,/^}/p' run.sh | grep -o 'MQTT_[A-Z]*=.*')"; set +a

    # What is missing, mesh-wide, changing nothing
    ./venv/bin/python3 zigbee2mqtt_importer/backfill_triggers.py

    # Narrow by friendly name (substring) or by vendor, both case-insensitive
    ./venv/bin/python3 zigbee2mqtt_importer/backfill_triggers.py --device "Ohana Bedroom Dummy"
    ./venv/bin/python3 zigbee2mqtt_importer/backfill_triggers.py --vendor Inovelli

    # Publish, once the dry run above looks right
    ./venv/bin/python3 zigbee2mqtt_importer/backfill_triggers.py --vendor Inovelli --live

Re-running is safe: a config that already agrees produces no write, so a second
pass over the same devices reports nothing to do.

A full mesh sweep takes several minutes - each device gets its own subscription
window, deliberately, because one broad `homeassistant/#` read silently returns
fewer retained configs than exist (see the note in `repoint.scan()`). Run it
detached rather than under a timeout::

    nohup ./venv/bin/python3 -u zigbee2mqtt_importer/backfill_triggers.py \\
        > /tmp/backfill.log 2>&1 &

`-u` matters: without it the output sits in Python's buffer and nothing is
readable until the run ends. Check progress with `tail -f /tmp/backfill.log`,
and note that `pgrep -f backfill_triggers.py` matches the shell running the
pgrep itself - use `pgrep -f "[b]ackfill_triggers.py"` to test whether it is
still going.
"""

import argparse

import triggers
import z2m


def plan_for(device, coordinator, version, base_topic):
    """Every missing or stale action config for one device.

    Planned under the name the device answers to now. Unlike the importer, this
    runs outside a migration, so there is no pending rename to build for.
    """
    ieee = device["ieee_address"]
    # Per device, deliberately - see the note in repoint.scan() about a broad
    # wildcard silently returning fewer retained configs than exist.
    retained = z2m.collect(
        f"{triggers.DISCOVERY_PREFIX}/device_automation/{ieee}/+/config"
    )
    return triggers.plan(
        device,
        z2m.exposes(device),
        retained,
        coordinator,
        version,
        base_topic,
        device["friendly_name"],
    )


def scan(match=None, vendor=None, live=False):
    """Backfill every device, or just those matching `match` and/or `vendor`."""
    bridge = z2m.info()
    base_topic = bridge["config"]["mqtt"]["base_topic"]
    devices = z2m.devices()

    coordinator = next(
        (
            device["ieee_address"]
            for device in devices
            if device.get("type") == "Coordinator"
        ),
        None,
    )
    if coordinator is None:
        raise SystemExit(
            "no coordinator in the bridge device list, cannot build trigger configs"
        )

    total = 0
    for device in devices:
        name = device.get("friendly_name")
        if not name or device.get("type") == "Coordinator":
            continue
        if match and match.lower() not in name.lower():
            continue
        if vendor:
            made_by = (device.get("definition") or {}).get("vendor") or ""
            if vendor.lower() not in made_by.lower():
                continue

        writes = plan_for(
            device, coordinator, bridge.get("version", "unknown"), base_topic
        )
        if not writes:
            continue

        total += len(writes)
        print(
            f"{name} ({device['ieee_address']}): {len(writes)} config(s) missing or stale"
        )
        for topic, _ in writes:
            print(f"    {topic.split('/')[-2]}")
        if live:
            z2m.publish_retained(
                [(topic, triggers.encode(payload)) for topic, payload in writes]
            )

    print(f"{total} config(s) {'published' if live else 'to publish, run with --live'}")
    return total


def main():
    parser = argparse.ArgumentParser(
        description="Publish the action trigger discovery configs Zigbee2MQTT has not written"
    )
    parser.add_argument(
        "--device",
        help="Only devices whose friendly name contains this (case-insensitive)",
    )
    parser.add_argument(
        "--vendor",
        help="Only devices from this vendor, e.g. Inovelli (case-insensitive)",
    )
    parser.add_argument("--live", action="store_true", help="Publish the configs")
    args = parser.parse_args()

    scan(match=args.device, vendor=args.vendor, live=args.live)


if __name__ == "__main__":
    main()

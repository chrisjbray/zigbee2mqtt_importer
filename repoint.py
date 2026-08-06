#!/usr/bin/env python3
"""Repoint retained discovery configs a rename left on a device's old identity.

Zigbee2MQTT writes a `device_automation` discovery config the first time an
action value is actually seen coming off the device, and never rewrites it
afterwards - not even when the device is renamed. A rename carrying
`homeassistant_rename` clears and republishes the *entity* configs, so those
come out clean, and the trigger configs are left exactly as they were. Every
config a device accumulated before its rename therefore still carries the old
`device.name` and still points `topic` at an action topic nothing publishes to
any more.

So the triggers are dead, and because Home Assistant takes a device's name
from whichever discovery message landed last, the device shows up as
`0x<ieee>` even though every one of its entity ids came out perfectly clean.

`triggers.py` repoints these as part of a migration now. This is the same fix
for devices migrated before it did, and for renames made outside this tool
entirely - in the Zigbee2MQTT UI, say, which has no idea any of this matters.

**Only discovery config topics are ever published.** The device's own action
topic is never touched: that one carries real events, and anything subscribed
to it would fire for real.
"""

import argparse

import triggers
import z2m


def repoint(ieee, friendly_name, retained, base_topic):
    """Return [(topic, payload)] for configs still on the pre-rename identity.

    Exactly two fields can go stale: the `device.name` Home Assistant takes the
    device's name from, and the `topic` a trigger listens on. Everything else
    is what Zigbee2MQTT wrote and is kept, so a config that already agrees
    produces no write at all and this is safe to re-run.

    Rewriting every occurrence of the IEEE in the payload instead would be
    wrong, not merely broader: `unique_id` and `identifiers` are *supposed* to
    contain the raw IEEE, and are what tie the config to its existing Home
    Assistant entity. Replacing it there would orphan the entity rather than
    rename it.
    """
    writes = []
    for topic, payload in sorted(retained.items()):
        device = payload.get("device") or {}
        patched = dict(payload)
        if device.get("name") == ieee:
            patched["device"] = dict(device, name=friendly_name)
        if ieee in (payload.get("topic") or ""):
            patched["topic"] = payload["topic"].replace(ieee, friendly_name)
        if patched != payload:
            writes.append((topic, patched))
    return writes


def scan(live=False):
    """Check every renamed device, and repoint the ones carrying stale configs."""
    base_topic = z2m.info()["config"]["mqtt"]["base_topic"]
    total = 0

    for device in z2m.devices():
        ieee, name = device.get("ieee_address"), device.get("friendly_name")
        if not ieee or not name or name == ieee:
            # Never renamed, so the raw identity in its configs is the right one.
            continue

        # Per device, deliberately. Subscribing to `homeassistant/#` once to
        # sweep the whole mesh in a single pass looks obviously cheaper and
        # silently loses messages: the retained burst is thousands of messages
        # and overruns the broker's queue for a QoS 0 subscriber, which reports
        # no error - it just hands back fewer configs than exist. Measured
        # against one device, the wildcard read returned 80 of its 115 configs
        # and none of the 21 stale ones, i.e. it reports a clean mesh whether
        # or not the mesh is clean. One subscription per device keeps each
        # burst small enough to arrive intact.
        retained = z2m.collect(f"{triggers.DISCOVERY_PREFIX}/+/{ieee}/+/config")
        writes = repoint(ieee, name, retained, base_topic)
        if not writes:
            continue

        total += len(writes)
        print(f"{name} ({ieee}): {len(writes)} of {len(retained)} config(s) stale")
        if live:
            z2m.publish_retained([(topic, triggers.encode(payload)) for topic, payload in writes])

    print(f"{total} config(s) {'repointed' if live else 'stale, run with --live to repoint'}")
    return total


def _self_check():
    ieee, name = "0x6c5cb1fffe56f477", "kg Bedroom Desk Sconce"
    stale_topic = f"homeassistant/device_automation/{ieee}/action_up_single/config"
    clean_topic = f"homeassistant/device_automation/{ieee}/action_down_single/config"

    def config(device_name, action_name):
        return {
            "automation_type": "trigger",
            "device": {
                "hw_version": 1,
                "identifiers": [f"zigbee2mqtt_{ieee}"],
                "name": device_name,
            },
            "payload": "up_single",
            "topic": f"zigbee2mqtt/{action_name}/action",
            "type": "action",
        }

    retained = {stale_topic: config(ieee, ieee), clean_topic: config(name, name)}
    writes = dict(repoint(ieee, name, retained, "zigbee2mqtt"))

    # Only the stale one is rewritten; one that already agrees is not touched.
    assert list(writes) == [stale_topic], writes
    assert writes[stale_topic]["device"]["name"] == name
    assert writes[stale_topic]["topic"] == f"zigbee2mqtt/{name}/action"

    # What only Zigbee2MQTT knew is kept, and the IEEE stays where it belongs:
    # in the fields that tie this config to its existing Home Assistant entity.
    assert writes[stale_topic]["device"]["hw_version"] == 1
    assert writes[stale_topic]["device"]["identifiers"] == [f"zigbee2mqtt_{ieee}"]
    assert writes[stale_topic]["payload"] == "up_single"

    # Re-running over the result is a no-op, so this is safe to run repeatedly.
    assert repoint(ieee, name, {stale_topic: writes[stale_topic]}, "zigbee2mqtt") == []

    # A device whose name is still its IEEE has nothing stale about it.
    assert repoint(ieee, ieee, retained, "zigbee2mqtt") == []

    print("repoint self-check OK")


def main():
    parser = argparse.ArgumentParser(
        description="Repoint retained discovery configs left on a device's pre-rename identity"
    )
    parser.add_argument("--live", action="store_true", help="Publish the repointed configs")
    parser.add_argument("--self-check", action="store_true", help="Run the self-check and exit")
    args = parser.parse_args()

    if args.self_check:
        _self_check()
        return
    scan(live=args.live)


if __name__ == "__main__":
    main()

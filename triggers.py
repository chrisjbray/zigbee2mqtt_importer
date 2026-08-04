#!/usr/bin/env python3
"""Pre-publish Home Assistant device trigger discovery for every action value.

Zigbee2MQTT only publishes the `device_automation` discovery config for an
action the first time that exact value is actually seen coming off the device:
`onZigbeeEvent` reads `data.message.action` and calls
`publishDeviceTriggerDiscover` for that one value. It never publishes the
static `exposes.action.values` enum up front. So until somebody physically
performs every gesture, most of a switch's triggers are not selectable in Home
Assistant at all. On this network a VZM31-SN advertises 21 action values and
typically only about ten have ever fired.

This publishes the missing configs directly, which is registration metadata
only.

**Only the discovery config topic is ever published.** The device's own
`<base>/<name>/action` topic is never touched: that one carries real events,
and anything already subscribed to it would fire for real.
"""

import json

DISCOVERY_PREFIX = "homeassistant"

# The only field of the device block that cannot be derived from bridge/devices
# is the hardware version, which Zigbee2MQTT reads out of zigbee-herdsman. Take
# it from an existing discovery config rather than reconstructing it.
INHERITED_DEVICE_FIELDS = ("hw_version",)


def config_topic(ieee_address, value):
    """The discovery topic for one action value. IEEE is the 0x form here."""
    return f"{DISCOVERY_PREFIX}/device_automation/{ieee_address}/action_{value}/config"


def action_values(exposes):
    """Every action this device can emit, from its own exposes definition."""
    action = exposes.get("action") or {}
    return list(action.get("values") or [])


def discovered_values(retained):
    """The action values that already have a discovery config on the broker.

    `retained` maps topic -> payload for the device's existing configs.
    """
    found = set()
    for topic in retained:
        name = topic.rsplit("/", 2)[-2] if topic.endswith("/config") else ""
        if name.startswith("action_"):
            found.add(name[len("action_") :])
    return found


def device_block(z2m_device, coordinator_ieee, template=None):
    """The `device` block, built from the bridge's own view of the device."""
    definition = z2m_device.get("definition") or {}
    block = {
        "identifiers": [f"zigbee2mqtt_{z2m_device['ieee_address']}"],
        "manufacturer": definition.get("vendor"),
        "model": definition.get("description"),
        "model_id": definition.get("model"),
        "name": z2m_device["friendly_name"],
        "via_device": f"zigbee2mqtt_bridge_{coordinator_ieee}",
    }
    if z2m_device.get("software_build_id"):
        block["sw_version"] = z2m_device["software_build_id"]

    for field in INHERITED_DEVICE_FIELDS:
        if template and field in template:
            block.setdefault(field, template[field])
    return block


def build_payload(value, friendly_name, device, origin, base_topic):
    """One discovery payload, matching what Zigbee2MQTT itself publishes."""
    return {
        "automation_type": "trigger",
        "device": device,
        "origin": origin,
        "payload": value,
        "subtype": value,
        "topic": f"{base_topic}/{friendly_name}/action",
        "type": "action",
    }


def encode(payload):
    """Serialise the way Zigbee2MQTT does, with sorted keys and no padding."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def plan(z2m_device, exposes, retained, coordinator_ieee, z2m_version, base_topic):
    """Return [(topic, payload)] for the action values that have no config yet.

    Values that already have a config are left alone: a real one published by
    Zigbee2MQTT after an actual button press is authoritative, and re-publishing
    would only risk replacing it with something less complete.
    """
    values = action_values(exposes)
    if not values:
        return []

    already = discovered_values(retained)
    template = next(iter(retained.values()), {}).get("device") if retained else None
    device = device_block(z2m_device, coordinator_ieee, template)
    origin = {"name": "Zigbee2MQTT", "sw": z2m_version, "url": "https://www.zigbee2mqtt.io"}

    return [
        (
            config_topic(z2m_device["ieee_address"], value),
            build_payload(value, z2m_device["friendly_name"], device, origin, base_topic),
        )
        for value in values
        if value not in already
    ]


def _self_check():
    device_entry = {
        "friendly_name": "Example Switch",
        "ieee_address": "0x6c5cb1fffe0a4c21",
        "software_build_id": "2.18",
        "definition": {
            "vendor": "Inovelli",
            "model": "VZM31-SN",
            "description": "2-in-1 switch + dimmer",
        },
    }
    exposes = {"action": {"property": "action", "type": "enum", "values": ["up_single", "down_single"]}}

    # A real retained config, verbatim off the broker.
    real_topic = "homeassistant/device_automation/0x6c5cb1fffe0a4c21/action_up_single/config"
    real = json.loads(
        '{"automation_type":"trigger","device":{"hw_version":1,"identifiers":'
        '["zigbee2mqtt_0x6c5cb1fffe0a4c21"],"manufacturer":"Inovelli","model":'
        '"2-in-1 switch + dimmer","model_id":"VZM31-SN","name":"Example Switch",'
        '"sw_version":"2.18","via_device":"zigbee2mqtt_bridge_0x00124b00a17f52d9"},'
        '"origin":{"name":"Zigbee2MQTT","sw":"2.7.1","url":"https://www.zigbee2mqtt.io"},'
        '"payload":"up_single","subtype":"up_single",'
        '"topic":"zigbee2mqtt/Example Switch/action","type":"action"}'
    )

    writes = plan(device_entry, exposes, {real_topic: real}, "0x00124b00a17f52d9", "2.7.1", "zigbee2mqtt")

    # up_single already has a config, so only down_single is planned.
    assert len(writes) == 1, writes
    topic, payload = writes[0]
    assert topic == "homeassistant/device_automation/0x6c5cb1fffe0a4c21/action_down_single/config"

    # Rebuilding the already-published one must reproduce it exactly, which is
    # what proves the payload contract is right.
    rebuilt = build_payload(
        "up_single",
        device_entry["friendly_name"],
        device_block(device_entry, "0x00124b00a17f52d9", real["device"]),
        {"name": "Zigbee2MQTT", "sw": "2.7.1", "url": "https://www.zigbee2mqtt.io"},
        "zigbee2mqtt",
    )
    assert encode(rebuilt) == encode(real), f"\n{encode(rebuilt)}\n{encode(real)}"

    # The action topic must never appear as something we publish to.
    assert all(t.startswith(f"{DISCOVERY_PREFIX}/device_automation/") for t, _ in writes)

    # A device with no action expose plans nothing.
    assert plan(device_entry, {}, {}, "0x0", "2.12.1", "zigbee2mqtt") == []

    print("triggers self-check OK")


if __name__ == "__main__":
    _self_check()

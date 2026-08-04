#!/usr/bin/env python3
"""Zigbee2MQTT bridge queries and device renames over MQTT.

New devices are detected by polling the retained `bridge/devices` topic rather
than by subscribing to `bridge/event`. `bridge/devices` is retained, so every
poll returns complete current state and nothing is missed while the tool is
restarted or stopped, whereas `device_joined` events are edge triggered and
lost if they fire while the tool is down. The trigger condition here is a set
intersection (a Z2M device whose IEEE is still in the ZHA snapshot) which is
naturally idempotent, so polling costs nothing in complexity.
"""

import json
import os
import threading
import time

import paho.mqtt.client as mqtt

BASE_TOPIC = os.getenv("Z2M_BASE_TOPIC", "zigbee2mqtt")
DEVICES_TOPIC = f"{BASE_TOPIC}/bridge/devices"
RENAME_REQUEST_TOPIC = f"{BASE_TOPIC}/bridge/request/device/rename"
RENAME_RESPONSE_TOPIC = f"{BASE_TOPIC}/bridge/response/device/rename"


def _client():
    """Connect a client to the broker described by the MQTT_* environment."""
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client()

    user = os.getenv("MQTT_USER", "")
    if user:
        client.username_pw_set(user, os.getenv("MQTT_PASSWORD", ""))
    client.connect(os.getenv("MQTT_HOST", "127.0.0.1"), int(os.getenv("MQTT_PORT", 1883)), 60)
    return client


def _await_message(topic, timeout, publish=None):
    """Subscribe, optionally publish a request, and return the first payload."""
    received = {}
    done = threading.Event()

    def on_message(client, userdata, message):
        received["payload"] = json.loads(message.payload.decode())
        done.set()

    client = _client()
    client.on_message = on_message
    client.subscribe(topic)
    client.loop_start()
    try:
        if publish:
            client.publish(publish[0], json.dumps(publish[1]))
        if not done.wait(timeout):
            raise TimeoutError(f"no message on {topic} within {timeout}s")
    finally:
        client.loop_stop()
        client.disconnect()
    return received["payload"]


def devices(timeout=15):
    """The current Zigbee2MQTT device list from the retained bridge topic."""
    return _await_message(DEVICES_TOPIC, timeout)


def paired_devices(timeout=15):
    """Map normalised IEEE -> the bridge's device entry, coordinator excluded."""
    return {
        device["ieee_address"].replace("0x", "").lower(): device
        for device in devices(timeout)
        if device.get("type") != "Coordinator"
    }


def exposes(device):
    """Flatten a bridge device entry's exposes to {property: definition}."""
    flat = {}

    def walk(items):
        for item in items:
            if "features" in item:
                walk(item["features"])
            elif item.get("property"):
                flat.setdefault(item["property"], item)

    walk((device.get("definition") or {}).get("exposes") or [])
    return flat


def set_attributes(friendly_name, payload, timeout=10):
    """Publish a settings payload to a device.

    Values are validated against the device's own exposes before they get here,
    which is what catches the realistic failures.
    """
    # ponytail: fire and forget, no state echo comparison. Add one if writes
    # ever start silently not applying.
    client = _client()
    client.loop_start()
    try:
        client.publish(f"{BASE_TOPIC}/{friendly_name}/set", json.dumps(payload)).wait_for_publish(timeout)
    finally:
        client.loop_stop()
        client.disconnect()


def info(timeout=15):
    """The bridge's own retained info topic."""
    return _await_message(f"{BASE_TOPIC}/bridge/info", timeout)


def collect(topic, window=3.0):
    """Gather every retained message currently on a wildcard topic.

    Retained messages all arrive immediately on subscribe, so a short window is
    enough; there is no completion signal to wait for.
    """
    found = {}

    def on_message(client, userdata, message):
        try:
            found[message.topic] = json.loads(message.payload.decode())
        except ValueError:
            pass

    client = _client()
    client.on_message = on_message
    client.subscribe(topic)
    client.loop_start()
    try:
        time.sleep(window)
    finally:
        client.loop_stop()
        client.disconnect()
    return found


def publish_retained(messages, timeout=10):
    """Publish retained QoS 1 messages, matching how Zigbee2MQTT publishes."""
    client = _client()
    client.loop_start()
    try:
        for topic, payload in messages:
            client.publish(topic, payload, qos=1, retain=True).wait_for_publish(timeout)
    finally:
        client.loop_stop()
        client.disconnect()


def rename(old_name, new_name, timeout=20):
    """Rename a device's friendly_name, verified against the bridge response.

    A rename to the name the device already has is not an error, it is nothing
    to do. Zigbee2MQTT rejects it outright with "friendly_name is already in
    use", which used to abort the whole migration part way through and leave it
    to be retried from the start on every pass.

    `homeassistant_rename` is deliberately left at its default of false: this
    tool renames the Home Assistant entities itself, and letting Z2M do it too
    would fight over the same entity ids.
    """
    if old_name == new_name:
        return None

    response = _await_message(
        RENAME_RESPONSE_TOPIC,
        timeout,
        publish=(RENAME_REQUEST_TOPIC, {"from": old_name, "to": new_name}),
    )
    if response.get("status") != "ok":
        raise RuntimeError(f"Z2M rename {old_name!r} -> {new_name!r} failed: {response}")
    return response


if __name__ == "__main__":
    for ieee, device in sorted(paired_devices().items(), key=lambda item: item[1]["friendly_name"]):
        print(f"{ieee}  {device['friendly_name']}")

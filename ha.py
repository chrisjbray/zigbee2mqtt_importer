#!/usr/bin/env python3
"""Home Assistant registry reads and live registry mutations.

Reads come straight from `.storage`; there is no REST endpoint for the device
or entity registries. Writes use whichever mechanism actually exists:

* entity_id renames go over REST (`homeassistant.update_entity_id`, provided by
  Spook, confirmed present on this instance),
* device renames, disables and area moves go over the websocket API
  (`config/device_registry/update`), which has no REST equivalent at all. That
  runs from inside the `homeassistant` container, which bundles the
  `websockets` library that the host virtualenv does not have.

Only the standard library is used for HTTP so this tool adds no dependency to
the shared virtualenv.
"""

import json
import os
import subprocess
import urllib.error
import urllib.request

CONFIG_DIR = os.getenv("HA_CONFIG_DIR", "/data/config/homeassistant")
STORAGE_DIR = os.path.join(CONFIG_DIR, ".storage")
BASE_URL = os.getenv("HA_URL", "http://127.0.0.1:8123")
TOKEN_FILE = os.getenv("HA_TOKEN_FILE", "/root/.ha_token_claude")
CONTAINER = os.getenv("HA_CONTAINER", "homeassistant")

_WS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ha_ws_call.py")
_WS_SCRIPT_IN_CONTAINER = "/tmp/zigbee2mqtt_importer_ws_call.py"

_token = None
_ws_script_copied = False


def token():
    """The long-lived access token. Root-readable only, so this needs sudo."""
    global _token
    if _token is None:
        with open(TOKEN_FILE) as handle:
            _token = handle.read().strip()
    return _token


def load_storage(name):
    """Load one of Home Assistant's `.storage` JSON files."""
    with open(os.path.join(STORAGE_DIR, name)) as handle:
        return json.load(handle)


def zha_entry_id():
    """Look the ZHA config entry id up dynamically rather than hardcoding it."""
    for entry in load_storage("core.config_entries")["data"]["entries"]:
        if entry["domain"] == "zha":
            return entry["entry_id"]
    raise RuntimeError("no ZHA config entry found in core.config_entries")


def areas():
    """Map area_id -> area name."""
    return {a["id"]: a["name"] for a in load_storage("core.area_registry")["data"]["areas"]}


def devices():
    """Every device registry entry."""
    return load_storage("core.device_registry")["data"]["devices"]


def entities():
    """Every entity registry entry."""
    return load_storage("core.entity_registry")["data"]["entities"]


def device_entities(all_entities, device_id):
    """The entity registry entries belonging to one device."""
    return [e for e in all_entities if e.get("device_id") == device_id]


def mqtt_device_by_ieee(all_devices, ieee):
    """Find the Zigbee2MQTT-created device registry entry for an IEEE address.

    Z2M's MQTT discovery registers the device as `zigbee2mqtt_0x<ieee>`, so
    match on the tail rather than on the whole identifier. An IEEE address is
    sixteen hex digits, which is specific enough for that to be unambiguous.
    """
    for device in all_devices:
        for identifier in device.get("identifiers") or []:
            if len(identifier) > 1 and identifier[0] == "mqtt":
                if str(identifier[1]).replace(":", "").lower().endswith(ieee):
                    return device
    return None


def states():
    """Current state of every entity, keyed by entity_id."""
    request = urllib.request.Request(f"{BASE_URL}/api/states", headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return {entity["entity_id"]: entity["state"] for entity in json.load(response)}


def call_service(domain, service, payload):
    """POST to the REST service endpoint, raising on any non-2xx response."""
    request = urllib.request.Request(
        f"{BASE_URL}/api/services/{domain}/{service}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or "[]")
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"{domain}.{service} failed: HTTP {error.code} {error.read().decode(errors='replace')[:200]}"
        ) from error


def update_entity_id(old_entity_id, new_entity_id):
    """Rename an entity_id in place, no restart needed.

    Renaming an entity to the id it already has is nothing to do, not an error.
    """
    if old_entity_id == new_entity_id:
        return None

    return call_service(
        "homeassistant",
        "update_entity_id",
        {"entity_id": old_entity_id, "new_entity_id": new_entity_id},
    )


def ws_call(messages):
    """Run websocket API commands from inside the Home Assistant container."""
    global _ws_script_copied
    if not _ws_script_copied:
        subprocess.run(
            ["docker", "cp", _WS_SCRIPT, f"{CONTAINER}:{_WS_SCRIPT_IN_CONTAINER}"],
            check=True,
            capture_output=True,
        )
        _ws_script_copied = True

    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", _WS_SCRIPT_IN_CONTAINER],
        input=json.dumps({"token": token(), "messages": messages}),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"websocket call failed: {result.stderr.strip()[:300]}")

    responses = json.loads(result.stdout)
    for message, response in zip(messages, responses):
        if not response.get("success"):
            raise RuntimeError(f"websocket command {message['type']} failed: {response.get('error')}")
    return responses


def update_device(device_id, **fields):
    """Update device registry fields (name_by_user, disabled_by, area_id)."""
    return ws_call([dict(fields, type="config/device_registry/update", device_id=device_id)])[0]


def reload_domains(domains):
    """Reload the config domains whose YAML we rewrote, avoiding a restart."""
    for domain in domains:
        call_service(domain, "reload", {})

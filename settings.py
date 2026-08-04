#!/usr/bin/env python3
"""Copy a device's settings from ZHA across to Zigbee2MQTT.

Scoped to the Inovelli VZM31-SN, which is the bulk of this migration. Any other
model is reported as having no settings map rather than quietly skipped.

The map is keyed by the ZHA **attribute** name, taken from the tail of the
entity's `unique_id`, not by the entity_id. On a real VZM31-SN the entity ids
cannot be trusted: three of them are called `..._none`, and the entity whose id
ends `_button_delay` is actually the local dimming up speed, mislabelled by a
ZHA quirk. The unique_id tail is the attribute the device itself reports.

Every planned write is checked against the target device's own `exposes`
definition before it is sent, so an out of range number or an enum value that
does not exist is reported rather than published and silently dropped.
"""

# ZHA attribute -> (Z2M property, rule). The rule is one of:
#   "number"  copy the value across as an integer
#   "text"    copy the value across as a string, the wording already matches
#   "index"   the ZHA value is an index into the Z2M enum's list of values
#   {...}     an explicit ZHA value -> Z2M value translation
VZM31_SN = {
    # Dimming and ramp rates, plain numbers on both sides.
    "dimming_speed_up_remote": ("dimmingSpeedUpRemote", "number"),
    "dimming_speed_up_local": ("dimmingSpeedUpLocal", "number"),
    "dimming_speed_down_remote": ("dimmingSpeedDownRemote", "number"),
    "dimming_speed_down_local": ("dimmingSpeedDownLocal", "number"),
    "ramp_rate_off_to_on_local": ("rampRateOffToOnLocal", "number"),
    "ramp_rate_off_to_on_remote": ("rampRateOffToOnRemote", "number"),
    "ramp_rate_on_to_off_local": ("rampRateOnToOffLocal", "number"),
    "ramp_rate_on_to_off_remote": ("rampRateOnToOffRemote", "number"),
    # Levels.
    "minimum_level": ("minimumLevel", "number"),
    "maximum_level": ("maximumLevel", "number"),
    "default_level_local": ("defaultLevelLocal", "number"),
    "default_level_remote": ("defaultLevelRemote", "number"),
    "state_after_power_restored": ("stateAfterPowerRestored", "number"),
    "auto_off_timer": ("autoTimerOff", "number"),
    # LED bar defaults. Z2M also has per-bar defaultLed1..7 variants; these are
    # the aggregate controls, which is what ZHA's "all LED" entities are.
    "led_color_when_on": ("ledColorWhenOn", "number"),
    "led_color_when_off": ("ledColorWhenOff", "number"),
    "led_intensity_when_on": ("ledIntensityWhenOn", "number"),
    "led_intensity_when_off": ("ledIntensityWhenOff", "number"),
    # ZHA exposes these as a number, Z2M as an enum of the same options in
    # order, so the ZHA value is the index of the Z2M value.
    "button_delay": ("buttonDelay", "index"),
    "load_level_indicator_timeout": ("loadLevelIndicatorTimeout", "index"),
    # Selects whose wording is already identical on both sides.
    "output_mode": ("outputMode", "text"),
    "switch_type": ("switchType", "text"),
    # Same setting, different wording.
    "leading_or_trailing_edge": (
        "dimmingMode",
        {"LeadingEdge": "Leading edge", "TrailingEdge": "Trailing edge"},
    ),
    # ZHA booleans against Z2M enums. The enum values are quoted exactly as the
    # device reports them in its exposes definition.
    "invert_switch": ("invertSwitch", {"off": "No", "on": "Yes"}),
    "smart_bulb_mode": ("smartBulbMode", {"off": "Disabled", "on": "Smart Bulb Mode"}),
    "local_protection": ("localProtection", {"off": "Disabled", "on": "Enabled"}),
    "firmware_progress_led": (
        "firmwareUpdateInProgressIndicator",
        {"off": "Disabled", "on": "Enabled"},
    ),
    # "Only 1 LED mode" on, means only one LED bar segment lights up.
    "on_off_led_mode": ("onOffLedMode", {"off": "All", "on": "One"}),
    # Both of these are phrased as "disable ..." in ZHA, and Z2M names the
    # parameter rather than the effect, so the polarity has to be read off the
    # Z2M value text rather than assumed. Z2M's "Enabled (Click Sound Off)"
    # means the parameter is on and therefore the click is silenced, which is
    # what ZHA calls "disable relay click" being on.
    "relay_click_in_on_off_mode": (
        "relayClick",
        {"off": "Disabled (Click Sound On)", "on": "Enabled (Click Sound Off)"},
    ),
    "disable_clear_notifications_double_tap": (
        "doubleTapClearNotifications",
        {"off": "Enabled (Default)", "on": "Disabled"},
    ),
}

# Deliberately not mapped, with the reason, so they are reported every time
# rather than looking like an oversight.
VZM31_SN_UNMAPPED = {
    "on_level": "standard ZCL level cluster attribute, Zigbee2MQTT does not expose it for this model",
    "on_off_transition_time": "standard ZCL level cluster attribute, Zigbee2MQTT does not expose it for this model",
    "start_up_current_level": "standard ZCL level cluster attribute, Zigbee2MQTT does not expose it for this model",
    "StartUpOnOff": "standard ZCL on/off cluster attribute; Zigbee2MQTT exposes no power_on_behavior for this model, "
    "and stateAfterPowerRestored is already carried by ZHA's own state_after_power_restored",
    "double_tap_up_enabled": "Zigbee2MQTT's doubleTapUpToParam55 looks related but configures what a double tap does, "
    "not whether it is enabled; not mapped on a name similarity",
    "double_tap_down_enabled": "Zigbee2MQTT's doubleTapDownToParam56 looks related but configures what a double tap "
    "does, not whether it is enabled; not mapped on a name similarity",
}

MODELS = {"VZM31-SN": (VZM31_SN, VZM31_SN_UNMAPPED)}

# Home Assistant uses these when an entity has no usable value.
NO_VALUE = ("unknown", "unavailable", "none", "")


def zha_attribute(entity):
    """The device attribute an entity represents, from its unique_id tail."""
    return str(entity.get("unique_id") or "").rsplit("-", 1)[-1]


def translate(value, rule, expose):
    """Convert a ZHA state string into the value Zigbee2MQTT expects."""
    if isinstance(rule, dict):
        if value not in rule:
            raise ValueError(f"no translation defined for {value!r}")
        return rule[value]
    if rule == "number":
        return int(float(value))
    if rule == "text":
        return value
    if rule == "index":
        options = expose.get("values") or []
        index = int(float(value))
        if not 0 <= index < len(options):
            raise ValueError(f"index {index} is outside the {len(options)} options Zigbee2MQTT offers")
        return options[index]
    raise ValueError(f"unknown rule {rule!r}")


def check(value, expose):
    """Reject anything the device would not accept, before it is published."""
    if not expose.get("access", 0) & 2:
        raise ValueError(f"{expose.get('property')} is not writable")

    options = expose.get("values")
    if options is not None and value not in options:
        raise ValueError(f"{value!r} is not one of {options}")

    low, high = expose.get("value_min"), expose.get("value_max")
    if options is None and low is not None and not low <= value <= high:
        raise ValueError(f"{value} is outside the accepted range {low}..{high}")


def plan(model, zha_values, exposes):
    """Work out the settings writes for one device.

    `zha_values` maps ZHA attribute -> current state string, `exposes` maps Z2M
    property -> its exposes entry. Returns (writes, notes), where notes are the
    things a human has to look at.
    """
    if model not in MODELS:
        return {}, [f"no settings map defined for model {model}, its settings were not migrated"]

    mapping, unmapped = MODELS[model]
    writes, notes = {}, []

    for attribute, value in sorted(zha_values.items()):
        if attribute in unmapped:
            notes.append(f"{attribute} ({value!r}) not migrated: {unmapped[attribute]}")
            continue
        if attribute not in mapping:
            notes.append(f"{attribute} ({value!r}) has no mapping for {model}, not migrated")
            continue
        if str(value).strip().lower() in NO_VALUE:
            notes.append(f"{attribute} has no readable value ({value!r}), not migrated")
            continue

        target, rule = mapping[attribute]
        expose = exposes.get(target)
        if expose is None:
            notes.append(f"{attribute} maps to {target}, which this device does not expose, not migrated")
            continue

        try:
            translated = translate(value, rule, expose)
            check(translated, expose)
        except ValueError as error:
            notes.append(f"{attribute} ({value!r}) -> {target} failed: {error}")
            continue

        writes[target] = translated

    return writes, notes


def _self_check():
    # The real exposes definitions, copied from a live VZM31-SN.
    exposes = {
        "dimmingSpeedUpRemote": {
            "property": "dimmingSpeedUpRemote",
            "access": 7,
            "value_min": 0,
            "value_max": 127,
        },
        "buttonDelay": {
            "property": "buttonDelay",
            "access": 7,
            "values": [
                "0ms",
                "100ms",
                "200ms",
                "300ms",
                "400ms",
                "500ms",
                "600ms",
                "700ms",
                "800ms",
                "900ms",
            ],
        },
        "loadLevelIndicatorTimeout": {
            "property": "loadLevelIndicatorTimeout",
            "access": 7,
            "values": [
                "Stay Off",
                "1 Second",
                "2 Seconds",
                "3 Seconds",
                "4 Seconds",
                "5 Seconds",
                "6 Seconds",
                "7 Seconds",
                "8 Seconds",
                "9 Seconds",
                "10 Seconds",
                "Stay On",
            ],
        },
        "onOffLedMode": {"property": "onOffLedMode", "access": 7, "values": ["All", "One"]},
        "relayClick": {
            "property": "relayClick",
            "access": 7,
            "values": ["Disabled (Click Sound On)", "Enabled (Click Sound Off)"],
        },
        "doubleTapClearNotifications": {
            "property": "doubleTapClearNotifications",
            "access": 7,
            "values": ["Enabled (Default)", "Disabled"],
        },
        "dimmingMode": {"property": "dimmingMode", "access": 7, "values": ["Leading edge", "Trailing edge"]},
        "minimumLevel": {"property": "minimumLevel", "access": 7, "value_min": 1, "value_max": 254},
    }

    writes, notes = plan(
        "VZM31-SN",
        {
            "dimming_speed_up_remote": "25",
            "button_delay": "5",
            "load_level_indicator_timeout": "11",
            "on_off_led_mode": "off",
            "relay_click_in_on_off_mode": "off",
            "disable_clear_notifications_double_tap": "off",
            "leading_or_trailing_edge": "LeadingEdge",
            "on_level": "255",
            "minimum_level": "unknown",
        },
        exposes,
    )

    assert writes["dimmingSpeedUpRemote"] == 25
    # ZHA numbers index into the Z2M enum, they are not the value itself.
    assert writes["buttonDelay"] == "500ms"
    assert writes["loadLevelIndicatorTimeout"] == "Stay On"
    # The enum is All/One, not All/1, so a raw copy would be rejected.
    assert writes["onOffLedMode"] == "All"
    # "disable relay click" off means the click is audible.
    assert writes["relayClick"] == "Disabled (Click Sound On)"
    # "disable clear notifications" off means clearing stays enabled.
    assert writes["doubleTapClearNotifications"] == "Enabled (Default)"
    assert writes["dimmingMode"] == "Leading edge"

    # And the inverse direction, which is the one most likely to be backwards.
    inverted, _ = plan(
        "VZM31-SN",
        {
            "on_off_led_mode": "on",
            "relay_click_in_on_off_mode": "on",
            "disable_clear_notifications_double_tap": "on",
        },
        exposes,
    )
    assert inverted["onOffLedMode"] == "One"
    assert inverted["relayClick"] == "Enabled (Click Sound Off)"
    assert inverted["doubleTapClearNotifications"] == "Disabled"

    assert "on_level" not in writes and any("on_level" in note for note in notes)
    assert "minimumLevel" not in writes and any("no readable value" in note for note in notes)

    # An out of range value is caught rather than published.
    bad, bad_notes = plan("VZM31-SN", {"dimming_speed_up_remote": "999"}, exposes)
    assert not bad and any("outside the accepted range" in note for note in bad_notes)

    # An unknown model is reported, not silently skipped.
    empty, model_notes = plan("SOME-OTHER-MODEL", {"invert_switch": "off"}, exposes)
    assert not empty and "no settings map defined for model SOME-OTHER-MODEL" in model_notes[0]

    print("settings self-check OK")


if __name__ == "__main__":
    _self_check()

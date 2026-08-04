#!/usr/bin/env python3
"""Copy a device's settings from ZHA across to Zigbee2MQTT.

Most of this maps itself. ZHA names an attribute `dimming_speed_up_remote`
where Zigbee2MQTT calls the same thing `dimmingSpeedUpRemote`, so the default
rule is to camelCase the ZHA attribute and look for that property in the target
device's own `exposes`. Only the names that genuinely disagree need declaring,
in `ALIASES` below.

The conversion is worked out the same way, from the target's `exposes`:

* a numeric property takes the number,
* an enum property whose values already contain the ZHA value takes it as is,
* an enum property given a ZHA number treats it as an index into the values,
* an enum property given a ZHA `on`/`off` is a boolean, which is only inferred
  when the first value is unambiguously the off state (`Disabled`, `No`, `Off`)
  and otherwise has to be declared in `TRANSLATIONS`, because a guessed
  polarity is silently wrong rather than loudly wrong.

**Adding a model is normally just adding its name to `SUPPORTED_MODELS`.** Run
a dry run afterwards: anything that did not map itself is reported by name with
the reason, and only those need an entry here. The model gate exists so an
untried device type is reported rather than blindly written to.

The map is keyed by the ZHA attribute name, taken from the tail of the entity's
`unique_id`, not by entity_id. On a real VZM31-SN the entity ids cannot be
trusted: three are literally called `..._none`, and the entity whose id ends
`_button_delay` is actually the local dimming up speed, mislabelled by a ZHA
quirk. The unique_id tail is the attribute the device itself reports.
"""

import re

SUPPORTED_MODELS = ("VZM31-SN", "VZM35-SN")

# ZHA attribute -> Zigbee2MQTT property, for names that do not camelCase into
# each other.
ALIASES = {
    "auto_off_timer": "autoTimerOff",
    "aux_switch_scenes": "auxSwitchUniqueScenes",
    "double_tap_up_level": "brightnessLevelForDoubleTapUp",
    "double_tap_down_level": "brightnessLevelForDoubleTapDown",
    "disable_clear_notifications_double_tap": "doubleTapClearNotifications",
    "firmware_progress_led": "firmwareUpdateInProgressIndicator",
    "leading_or_trailing_edge": "dimmingMode",
    "relay_click_in_on_off_mode": "relayClick",
}

# Explicit ZHA value -> Zigbee2MQTT value, where the wording differs or the
# polarity cannot be inferred. A value may list several candidates, and the
# first one this device actually offers wins, so a single table serves every
# model: a switch and a fan both call the setting `output_mode` and each picks
# its own wording for it.
TRANSLATIONS = {
    "invert_switch": {"off": "No", "on": "Yes"},
    # "Only 1 LED mode" on means one bar segment lights, not all of them.
    "on_off_led_mode": {"off": "All", "on": "One"},
    # Reads as "disable X" in ZHA while Zigbee2MQTT names the parameter rather
    # than the effect, so the polarity comes from the Z2M wording.
    "disable_clear_notifications_double_tap": {"off": "Enabled (Default)", "on": "Disabled"},
    "leading_or_trailing_edge": {"LeadingEdge": "Leading edge", "TrailingEdge": "Trailing edge"},
    # Fan wording.
    "output_mode": {"OnOff": ["On/Off", "Exhaust Fan (On/Off)"], "Fan": ["Ceiling Fan (3-Speed)"]},
    "switch_type": {
        "Three Way AUX": ["Aux Switch", "3-Way Aux Switch"],
        "Three Way Dumb": ["3-Way Dumb Switch"],
    },
}

# Deliberately left alone, with the reason, so they read as a decision rather
# than an oversight.
UNMAPPED = {
    "on_level": "standard ZCL level cluster attribute, Zigbee2MQTT does not expose it",
    "on_off_transition_time": "standard ZCL level cluster attribute, Zigbee2MQTT does not expose it",
    "start_up_current_level": "standard ZCL level cluster attribute, Zigbee2MQTT does not expose it",
    "StartUpOnOff": "standard ZCL on/off cluster attribute; Zigbee2MQTT exposes no power_on_behavior, "
    "and stateAfterPowerRestored is already carried by ZHA's own state_after_power_restored",
    "double_tap_up_enabled": "Zigbee2MQTT's doubleTapUpToParam55 looks related but configures what a double tap "
    "does, not whether it is enabled; not mapped on a name similarity",
    "double_tap_down_enabled": "Zigbee2MQTT's doubleTapDownToParam56 looks related but configures what a double "
    "tap does, not whether it is enabled; not mapped on a name similarity",
    "smart_fan_mode": "no clear Zigbee2MQTT equivalent for the fan smart mode; not mapped on a guess",
    "smart_fan_led_display_levels": "ZHA reports a word, Zigbee2MQTT's fanLedLevelType wants a number, and the "
    "correspondence is not documented; not mapped on a guess",
}

# Home Assistant uses these when an entity has no usable value.
NO_VALUE = ("unknown", "unavailable", "none", "")

# Enum values that unambiguously mean "off", so a boolean can be inferred.
OFF_VALUES = ("disabled", "no", "off")


def zha_attribute(entity):
    """The device attribute an entity represents, from its unique_id tail."""
    return str(entity.get("unique_id") or "").rsplit("-", 1)[-1]


def camel_case(attribute):
    """`dimming_speed_up_remote` -> `dimmingSpeedUpRemote`."""
    head, *rest = attribute.split("_")
    return head + "".join(word[:1].upper() + word[1:] for word in rest)


def target_property(attribute, exposes):
    """The Zigbee2MQTT property an attribute belongs to, if the device has it."""
    name = ALIASES.get(attribute) or camel_case(attribute)
    return name if name in exposes else None


def is_number(value):
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", str(value).strip()))


def translate(value, expose, attribute):
    """Convert a ZHA state string into the value Zigbee2MQTT expects."""
    declared = TRANSLATIONS.get(attribute, {})
    options = expose.get("values")

    # A declared translation wins, but only where it fits this device's enum.
    if value in declared:
        candidates = declared[value]
        candidates = [candidates] if isinstance(candidates, str) else candidates
        for candidate in candidates:
            if options is None or candidate in options:
                return candidate

    if options is None:
        if not is_number(value):
            raise ValueError(f"{value!r} is not a number")
        return int(float(value))

    if value in options:
        return value

    if is_number(value):
        index = int(float(value))
        if not 0 <= index < len(options):
            raise ValueError(f"index {index} is outside the {len(options)} options Zigbee2MQTT offers")
        return options[index]

    if value in ("on", "off") and len(options) == 2:
        # Compare the leading word, so "Disabled (Click Sound On)" counts.
        if re.split(r"[\s(]", options[0].strip())[0].lower() not in OFF_VALUES:
            raise ValueError(
                f"boolean against enum {options}, and {options[0]!r} is not clearly the off state; "
                f"add an entry for {attribute!r} to TRANSLATIONS"
            )
        return options[0] if value == "off" else options[1]

    raise ValueError(f"no way to convert {value!r} to one of {options}")


def check(value, expose):
    """Reject anything the device would not accept, before it is published."""
    if not expose.get("access", 0) & 2:
        raise ValueError(f"{expose.get('property')} is not writable")

    options = expose.get("values")
    if options is not None:
        if value not in options:
            raise ValueError(f"{value!r} is not one of {options}")
        return

    low, high = expose.get("value_min"), expose.get("value_max")
    if low is not None and not low <= value <= high:
        raise ValueError(f"{value} is outside the accepted range {low}..{high}")


def plan(model, zha_values, exposes):
    """Work out the settings writes for one device.

    `zha_values` maps ZHA attribute -> value, `exposes` maps Z2M property -> its
    exposes entry. Returns (writes, notes), the notes being what a human has to
    look at.
    """
    if model not in SUPPORTED_MODELS:
        return {}, [f"no settings map defined for model {model}, its settings were not migrated"]

    writes, notes = {}, []
    for attribute, value in sorted(zha_values.items()):
        if attribute in UNMAPPED:
            notes.append(f"{attribute} ({value!r}) not migrated: {UNMAPPED[attribute]}")
            continue
        if str(value).strip().lower() in NO_VALUE:
            notes.append(f"{attribute} has no readable value ({value!r}), not migrated")
            continue

        target = target_property(attribute, exposes)
        if target is None:
            notes.append(
                f"{attribute} ({value!r}) has no matching property on this {model}, not migrated. "
                f"Add it to ALIASES or UNMAPPED once you know which it is"
            )
            continue

        try:
            translated = translate(value, exposes[target], attribute)
            check(translated, exposes[target])
        except ValueError as error:
            notes.append(f"{attribute} ({value!r}) -> {target} failed: {error}")
            continue

        writes[target] = translated

    return writes, notes


def _self_check():
    assert camel_case("dimming_speed_up_remote") == "dimmingSpeedUpRemote"
    assert camel_case("minimum_level") == "minimumLevel"

    # Real exposes definitions, taken from live devices.
    switch = {
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
        "onOffLedMode": {"property": "onOffLedMode", "access": 7, "values": ["All", "One"]},
        "invertSwitch": {"property": "invertSwitch", "access": 7, "values": ["Yes", "No"]},
        "localProtection": {"property": "localProtection", "access": 7, "values": ["Disabled", "Enabled"]},
        "relayClick": {
            "property": "relayClick",
            "access": 7,
            "values": ["Disabled (Click Sound On)", "Enabled (Click Sound Off)"],
        },
        "outputMode": {"property": "outputMode", "access": 7, "values": ["Dimmer", "On/Off"]},
        "minimumLevel": {"property": "minimumLevel", "access": 7, "value_min": 1, "value_max": 254},
    }

    writes, notes = plan(
        "VZM31-SN",
        {
            "dimming_speed_up_remote": "25",  # camelCases itself
            "button_delay": "5",  # number into an enum, by index
            "on_off_led_mode": "off",  # declared, All/One is not inferable
            "invert_switch": "off",  # declared, Yes/No is the wrong way round
            "local_protection": "off",  # inferred, Disabled is clearly off
            "relay_click_in_on_off_mode": "off",  # aliased, then inferred
            "output_mode": "Dimmer",  # already the right word
            "on_level": "255",  # deliberately unmapped
            "minimum_level": "unknown",  # no usable value
        },
        switch,
    )
    assert writes["dimmingSpeedUpRemote"] == 25
    assert writes["buttonDelay"] == "500ms"
    assert writes["onOffLedMode"] == "All"
    assert writes["invertSwitch"] == "No"
    assert writes["localProtection"] == "Disabled"
    assert writes["relayClick"] == "Disabled (Click Sound On)"
    assert writes["outputMode"] == "Dimmer"
    assert "on_level" not in writes and any("on_level" in note for note in notes)
    assert "minimumLevel" not in writes and any("no readable value" in note for note in notes)

    inverted, _ = plan(
        "VZM31-SN",
        {"on_off_led_mode": "on", "relay_click_in_on_off_mode": "on", "invert_switch": "on"},
        switch,
    )
    assert inverted["onOffLedMode"] == "One"
    assert inverted["relayClick"] == "Enabled (Click Sound Off)"
    assert inverted["invertSwitch"] == "Yes"

    # The fan reuses the same declarations but resolves them to its own wording.
    fan = {
        "outputMode": {
            "property": "outputMode",
            "access": 7,
            "values": ["Ceiling Fan (3-Speed)", "Exhaust Fan (On/Off)"],
        },
        "switchType": {"property": "switchType", "access": 7, "values": ["Single Pole", "Aux Switch"]},
        "smartBulbMode": {"property": "smartBulbMode", "access": 7, "values": ["Disabled", "Smart Fan Mode"]},
        "auxSwitchUniqueScenes": {
            "property": "auxSwitchUniqueScenes",
            "access": 7,
            "values": ["Disabled", "Enabled"],
        },
        "brightnessLevelForDoubleTapUp": {
            "property": "brightnessLevelForDoubleTapUp",
            "access": 7,
            "value_min": 2,
            "value_max": 255,
        },
        "quickStartTime": {"property": "quickStartTime", "access": 7, "value_min": 0, "value_max": 60},
    }
    fan_writes, fan_notes = plan(
        "VZM35-SN",
        {
            "output_mode": "OnOff",
            "switch_type": "Three Way AUX",
            "smart_bulb_mode": "off",
            "aux_switch_scenes": "off",
            "double_tap_up_level": "254",
            "quick_start_time": "0",
            "smart_fan_mode": "off",
        },
        fan,
    )
    assert fan_writes["outputMode"] == "Exhaust Fan (On/Off)"
    assert fan_writes["switchType"] == "Aux Switch"
    # Same declaration as the switch, but the fan calls it Smart Fan Mode.
    assert fan_writes["smartBulbMode"] == "Disabled"
    assert fan_writes["auxSwitchUniqueScenes"] == "Disabled"
    assert fan_writes["brightnessLevelForDoubleTapUp"] == 254
    assert fan_writes["quickStartTime"] == 0
    assert any("smart_fan_mode" in note for note in fan_notes)

    # Out of range is caught rather than published.
    bad, bad_notes = plan("VZM31-SN", {"dimming_speed_up_remote": "999"}, switch)
    assert not bad and any("outside the accepted range" in note for note in bad_notes)

    # An unknown model is reported, not silently skipped.
    empty, model_notes = plan("SOME-OTHER-MODEL", {"invert_switch": "off"}, switch)
    assert not empty and "no settings map defined for model SOME-OTHER-MODEL" in model_notes[0]

    print("settings self-check OK")


if __name__ == "__main__":
    _self_check()

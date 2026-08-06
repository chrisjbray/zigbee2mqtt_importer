#!/usr/bin/env python3
"""Derive the canonical `<Area> <Location> <Use>` name for a migrated device.

The Area component always comes from the old ZHA device's Home Assistant area.
Location and Use have to be guessed out of the free-form ZHA device name, which
is genuinely ambiguous, so anything that is not a clean two-token split is
reported as uncertain and left for manual review. `name_overrides.json` in the
working directory is the escape hatch: a normalised-IEEE -> canonical-name map
that always wins over the guess.
"""

import difflib
import json
import os
import re
import tempfile


def slug(text):
    """Slugify text the way Home Assistant slugifies entity object ids."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def _strip_area_words(tokens, area_name):
    """Drop leading name tokens that just repeat words from the area name."""
    area_words = {word.lower() for word in area_name.split()}
    while tokens and tokens[0].lower() in area_words:
        tokens = tokens[1:]
    return tokens


def derive(zha_name, area_name):
    """Return (canonical_name, uncertainty_reason). Either may be None."""
    if not area_name:
        return None, "device has no Home Assistant area, cannot build the Area component"

    tokens = _strip_area_words(zha_name.split(), area_name)

    if len(tokens) == 2:
        return f"{area_name} {tokens[0]} {tokens[1]}", None

    if len(tokens) < 2:
        return (
            None,
            f"only {len(tokens)} token(s) remain after removing area words "
            f"from {zha_name!r}, need a Location and a Use",
        )

    location, use = tokens[0], " ".join(tokens[1:])
    return (
        f"{area_name} {location} {use}",
        f"{len(tokens)} tokens remain in {zha_name!r}; guessed "
        f"Location={location!r} Use={use!r}, confirm before going live",
    )


TEMPLATE_PREFIX = "TODO confirm: "
ZHA_NAME_SUFFIX = "__zha_name"

_HELP = (
    "Canonical device names, keyed by IEEE address with no 0x and no colons. "
    f"A value still carrying the '{TEMPLATE_PREFIX}' prefix is this tool's own guess "
    "and is ignored: correct it if it is wrong, then delete that prefix to confirm "
    "it. Format: <Area> <Location> <Use>. A pending entry also gets a sibling key "
    f"'<ieee>{ZHA_NAME_SUFFIX}' showing the original ZHA device name it was guessed "
    "from - reference only, not read by the tool, safe to leave in place or delete "
    "once confirmed."
)


def closest_entity_id(entity_id, candidates, cutoff=0.6):
    """The candidate entity_id that looks most like this one, if any is close.

    Only ever used to name a likely counterpart in the review log; a match
    across integrations is never applied automatically. ZHA and Z2M can name
    the same physical sensor differently, so the trailing token is dropped
    before comparing: ZHA calls a sensor `..._motion` where Z2M's converter for
    the same hardware calls it `..._occupancy`. Candidates are restricted to
    the same domain, which is what separates the occupancy binary_sensor from
    the battery sensor once both have been stripped to the same stem.
    """
    domain, _, _ = entity_id.partition(".")

    def stem(value):
        return value.split(".", 1)[-1].rsplit("_", 1)[0]

    target = stem(entity_id)
    best, score = None, 0.0
    for candidate in candidates:
        if not candidate.startswith(f"{domain}."):
            continue
        ratio = difflib.SequenceMatcher(None, target, stem(candidate)).ratio()
        if ratio > score:
            best, score = candidate, ratio

    return (best, score) if best and score >= cutoff else None


def load_overrides(path):
    """Load the normalised-IEEE -> canonical-name override map, if present."""
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        return json.load(handle)


def canonical_for(ieee, zha_name, area_name, overrides):
    """Resolve the canonical name, preferring a confirmed override."""
    override = overrides.get(ieee)
    if override and not override.startswith(TEMPLATE_PREFIX):
        return override, None
    return derive(zha_name, area_name)


def write_template(path, ieee, proposed, zha_name=None):
    """Leave a prefilled entry so a name only has to be edited, not written.

    Returns True if one was added, False if it was already there. The prefix is
    what stops the tool from later acting on its own guess. zha_name, if given,
    is written to a separate sibling key rather than folded into the guessed
    name itself - canonical_for() treats any override not starting with
    TEMPLATE_PREFIX as final and confirmed, so embedding it in the same string
    would risk it surviving into a live device name if someone only strips the
    prefix and not a trailing annotation too.
    """
    overrides = load_overrides(path)
    if ieee in overrides:
        return False

    overrides.setdefault("_help", _HELP)
    overrides[ieee] = TEMPLATE_PREFIX + proposed
    if zha_name:
        overrides[ieee + ZHA_NAME_SUFFIX] = zha_name
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(overrides, handle, indent=2, sort_keys=True)
    return True


def _self_check():
    assert slug("Living Room Arch Sconces") == "living_room_arch_sconces"
    assert slug("kg Bathroom Sink Cans") == "kg_bathroom_sink_cans"

    # Clean two-token splits, with and without the area repeated in the name.
    assert derive("Garage Overhead Middle", "Garage") == ("Garage Overhead Middle", None)
    assert derive("Bathroom Closet Right", "kg Bathroom") == (
        "kg Bathroom Closet Right",
        None,
    )

    # Ambiguous: best-effort guess, but flagged.
    name, reason = derive("Hot Water Solar Pump", "Garage")
    assert name == "Garage Hot Water Solar Pump"
    assert reason

    # Not derivable at all.
    assert derive("Weather", "kg Closet")[0] is None
    assert derive("Anything", "")[0] is None

    # A confirmed override always wins and is never uncertain.
    assert canonical_for("abc", "Hot Water Solar Pump", "Garage", {"abc": "Garage Solar Pump"}) == (
        "Garage Solar Pump",
        None,
    )

    # An unconfirmed template must NOT be acted on, or the tool would end up
    # confirming its own guess.
    still_a_guess = canonical_for(
        "abc", "Hot Water Solar Pump", "Garage", {"abc": TEMPLATE_PREFIX + "Garage Solar Pump"}
    )
    assert still_a_guess[0] == "Garage Hot Water Solar Pump"
    assert still_a_guess[1]
    # The real case: ZHA exposed this sensor as motion, Z2M's converter for the
    # same hardware exposes it as occupancy, so no exact match exists. The
    # battery sensor strips to the same stem and is only ruled out by domain.
    suggestion = closest_entity_id(
        "binary_sensor.closet_motion_sensor_motion",
        [
            "sensor.garage_closet_motion_battery",
            "binary_sensor.garage_closet_motion_occupancy",
            "sensor.garage_closet_motion_linkquality",
        ],
    )
    assert suggestion is not None
    assert suggestion[0] == "binary_sensor.garage_closet_motion_occupancy", suggestion

    # Nothing related at all stays unsuggested rather than guessing.
    assert closest_entity_id("binary_sensor.front_door_contact", ["binary_sensor.kitchen_fan_state"]) is None

    # write_template() records the old ZHA name under a sibling key, not
    # folded into the guessed name itself, and canonical_for() must keep
    # ignoring that sibling key when resolving the real override.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "name_overrides.json")
        added = write_template(path, "abc", "Garage Hot Water Solar Pump", zha_name="Hot Water Solar Pump")
        assert added
        overrides = load_overrides(path)
        assert overrides["abc"] == TEMPLATE_PREFIX + "Garage Hot Water Solar Pump"
        assert overrides["abc" + ZHA_NAME_SUFFIX] == "Hot Water Solar Pump"
        # The sibling key sitting in the same dict must not change how the
        # real ieee's own override resolves.
        resolved = canonical_for("abc", "Hot Water Solar Pump", "Garage", overrides)
        assert resolved[0] == "Garage Hot Water Solar Pump"
        assert resolved[1]  # still an unconfirmed guess

    print("naming self-check OK")


if __name__ == "__main__":
    _self_check()

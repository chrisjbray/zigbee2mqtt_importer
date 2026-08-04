#!/usr/bin/env python3
"""Derive the canonical `[Area] [Location] [Use]` name for a migrated device.

The Area component always comes from the old ZHA device's Home Assistant area.
Location and Use have to be guessed out of the free-form ZHA device name, which
is genuinely ambiguous, so anything that is not a clean two-token split is
reported as uncertain and left for manual review. `name_overrides.json` in the
working directory is the escape hatch: a normalised-IEEE -> canonical-name map
that always wins over the guess.
"""

import json
import os
import re


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
        return None, "device has no Home Assistant area, cannot build the [Area] component"

    tokens = _strip_area_words(zha_name.split(), area_name)

    if len(tokens) == 2:
        return f"[{area_name}] [{tokens[0]}] [{tokens[1]}]", None

    if len(tokens) < 2:
        return (
            None,
            f"only {len(tokens)} token(s) remain after removing area words "
            f"from {zha_name!r}, need a Location and a Use",
        )

    location, use = tokens[0], " ".join(tokens[1:])
    return (
        f"[{area_name}] [{location}] [{use}]",
        f"{len(tokens)} tokens remain in {zha_name!r}; guessed "
        f"Location={location!r} Use={use!r}, confirm before going live",
    )


def load_overrides(path):
    """Load the normalised-IEEE -> canonical-name override map, if present."""
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        return json.load(handle)


def canonical_for(ieee, zha_name, area_name, overrides):
    """Resolve the canonical name, preferring an explicit override."""
    if ieee in overrides:
        return overrides[ieee], None
    return derive(zha_name, area_name)


def _self_check():
    assert slug("[Living Room] [Arch] [Sconces]") == "living_room_arch_sconces"
    assert slug("[kg Bathroom] [Sink] [Cans]") == "kg_bathroom_sink_cans"

    # Clean two-token splits, with and without the area repeated in the name.
    assert derive("Garage Overhead Middle", "Garage") == ("[Garage] [Overhead] [Middle]", None)
    assert derive("Bathroom Closet Right", "kg Bathroom") == (
        "[kg Bathroom] [Closet] [Right]",
        None,
    )

    # Ambiguous: best-effort guess, but flagged.
    name, reason = derive("Hot Water Solar Pump", "Garage")
    assert name == "[Garage] [Hot] [Water Solar Pump]"
    assert reason

    # Not derivable at all.
    assert derive("Weather", "kg Closet")[0] is None
    assert derive("Anything", "")[0] is None

    # An override always wins and is never uncertain.
    assert canonical_for("abc", "Hot Water Solar Pump", "Garage", {"abc": "[Garage] [Solar] [Pump]"}) == (
        "[Garage] [Solar] [Pump]",
        None,
    )
    print("naming self-check OK")


if __name__ == "__main__":
    _self_check()

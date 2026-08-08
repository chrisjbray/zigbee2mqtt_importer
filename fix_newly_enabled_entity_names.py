#!/usr/bin/env python3
"""fix_newly_enabled_entity_names.py — rename newly enabled 0x… entities to their suggested clean id.

When a disabled diagnostic (e.g. sensor.0x282c02bfffec4ff4_power_factor) is
re-enabled in HA, the entity_id stays raw. The registry already holds the
correct target in `suggested_object_id` (e.g. ohana_refrigerator_power_factor).
This script renames them via the websocket entity_registry API so history is
kept (no delete/recreate). Default is dry-run.

Usage:
  fix_newly_enabled_entity_names.py                  # dry-run, show what would change
  fix_newly_enabled_entity_names.py --live           # rename
  fix_newly_enabled_entity_names.py --live --only 0x282c02bfffec4ff4
  fix_newly_enabled_entity_names.py --live -v        # verbose websocket replies

Requires: running on vm101-pve01-hnl1, `websockets` inside the `homeassistant`
container (already there), and /root/.ha_token_claude.

Per AGENTS.md feedback: saved to /data/local/bin/claude/ on the host if present.
"""
import argparse
import asyncio
import json
import pathlib
import re
import subprocess
import sys

REGISTRY = pathlib.Path("/data/config/homeassistant/.storage/core.entity_registry")
PREFIX = "sensor.0x"
TOKEN_PATH = pathlib.Path("/root/.ha_token_claude")


def load_registry(path: pathlib.Path):
    data = json.loads(path.read_text())
    # HA stores entities at data.entities
    ents = data.get("data", {}).get("entities", data.get("entities", []))
    return ents, data


def candidates(entities, only: str | None):
    """Yield (entity_id, suggested_object_id, domain) for enabled 0x… that have a suggestion."""
    for e in entities:
        eid = e.get("entity_id", "")
        if not eid.startswith(PREFIX):
            continue
        if only and only not in eid:
            continue
        # Only newly enabled ones: disabled_by is null/absent now, was diagnostic+disabled before
        # Broadest safe: any enabled 0x… with a suggested_object_id and no underscore-pipe collision
        disabled = e.get("disabled_by")
        if disabled is not None:
            continue
        suggested = (e.get("suggested_object_id") or "").strip()
        if not suggested:
            continue
        # Never clobber an existing suggested collision naively — caller should check existence
        # Build target entity_id from domain + suggested_object_id
        domain = eid.split(".", 1)[0]
        target = f"{domain}.{suggested}"
        yield e, target


def build_new_map(entities, only):
    """Return list of (old, new, entity_dict) for rename."""
    out = []
    existing_ids = {e["entity_id"] for e in entities}
    for e, target in candidates(entities, only):
        old = e["entity_id"]
        if old == target:
            continue
        if target in existing_ids:
            # Collision — skip and warn, user should handle manually (maybe _2)
            print(f"SKIP collision: {old} -> {target} already exists", file=sys.stderr)
            continue
        out.append((old, target, e))
    return out


def rename_via_websocket(mapping: list[tuple[str, str, dict]], verbose: bool):
    token = subprocess.check_output(["sudo", "cat", str(TOKEN_PATH)], text=True).strip()
    uri = "ws://localhost:8123/api/websocket"
    # We need to run the client *inside* the homeassistant container where websockets lives,
    # but we already have it on the host's ansible venv. Easiest: docker exec a tiny python.
    # Instead of importing here, use docker exec with the token injected.
    lines = []
    for old, new, _ in mapping:
        # Use docker exec homeassistant python3 -c "..." with the pair
        payload = json.dumps(
            {"type": "config/entity_registry/update", "entity_id": old, "new_entity_id": new}
        )
        # Escape for shell: write to a temp file and exec
        lines.append((old, new, payload))
    # Single docker exec session that loops over all renames
    script = "import asyncio, json, websockets\n"
    script += f"token={token!r}\n"
    script += "pairs=[\n"
    for old, new, _ in mapping:
        script += f"  ({old!r}, {new!r}),\n"
    script += "]\n"
    script += """
async def run():
    uri='ws://localhost:8123/api/websocket'
    async with websockets.connect(uri) as ws:
        await ws.recv()
        await ws.send(json.dumps({'type':'auth','access_token': token}))
        print(await ws.recv())
        msg_id = 1
        for old, new in pairs:
            msg_id += 1
            await ws.send(json.dumps({'id': msg_id, 'type':'config/entity_registry/update','entity_id': old,'new_entity_id': new}))
            resp = await ws.recv()
            print(resp)
asyncio.run(run())
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", "homeassistant", "python3", "-"],
        input=script,
        text=True,
        capture_output=True,
    )
    if verbose:
        print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
    else:
        # Filter to auth + per-entity lines
        for line in proc.stdout.splitlines():
            print(line)
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main():
    ap = argparse.ArgumentParser(description="Rename newly enabled 0x... entities to suggested ids")
    ap.add_argument("--live", action="store_true", help="actually rename (default dry-run)")
    ap.add_argument("--only", help="only entities containing this substring (e.g. 0x282c02bfffec4ff4)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not REGISTRY.exists():
        print(f"Registry not found: {REGISTRY}", file=sys.stderr)
        sys.exit(2)

    entities, _ = load_registry(REGISTRY)
    mapping = build_new_map(entities, args.only)

    if not mapping:
        print("No enabled 0x... entities with a suggested_object_id to rename.")
        # Also report disabled count for context
        disabled_hex = [e for e in entities if e.get("entity_id","").startswith(PREFIX) and e.get("disabled_by")]
        if disabled_hex:
            print(f"  ({len(disabled_hex)} still disabled 0x... diagnostics — leave them until enabled)")
        return

    for old, new, e in mapping:
        sug = e.get("suggested_object_id")
        platform = e.get("platform")
        print(f"{old} -> {new}  (suggested {sug!r}, platform {platform})")

    if not args.live:
        print(f"\nDry-run: {len(mapping)} would be renamed. Re-run with --live.")
        return

    try:
        subprocess.check_output(["sudo", "cat", str(TOKEN_PATH)], text=True)
    except subprocess.CalledProcessError:
        print(f"Token not readable via sudo cat: {TOKEN_PATH}", file=sys.stderr)
        sys.exit(2)

    # Try to import websockets in container via docker exec
    try:
        rename_via_websocket(mapping, args.verbose)
    except FileNotFoundError:
        print("docker not found", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

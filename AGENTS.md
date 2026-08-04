# Zigbee2MQTT Importer

Finish the migration of Zigbee devices re-paired out of Home Assistant's ZHA
integration into Zigbee2MQTT.

## Development Rules

- **Formatting:** Use `black` for all Python code formatting.
- **Environment:** Use the shared virtual environment located in the parent directory: `../venv/`.
- **Dependencies:** Only the standard library for HTTP. Do not add dependencies to the shared venv without a good reason.
- **Commits:** ALWAYS ask for permission before committing if the repository root is the home directory.
- **Tracking:** Keep the `TODO.md` file updated with progress and remaining tasks.

## Key Logic

- **Safety:** Dry run is the default. Nothing is modified without `--live`, which requires root.
- **Detection:** Polls the retained `zigbee2mqtt/bridge/devices` topic; a device is a candidate when its IEEE is in both Z2M and the current ZHA snapshot.
- **Naming:** `[Area] [Location] [Use]`. The area always comes from the Home Assistant area registry, never from the device name.
- **Uncertainty:** An ambiguous name derivation is logged to `workdir/needs_review.log` and skipped, never guessed live. `workdir/name_overrides.json` is the manual override.
- **Registries:** Reads come from `.storage`. Entity id renames go over REST, device renames and disables over the websocket API, which has no REST equivalent.

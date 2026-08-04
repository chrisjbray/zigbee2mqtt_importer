# TODO

## Build

- [x] Periodic ZHA device snapshot, with the already-migrated exclusion filter
- [x] IEEE normalisation and ZHA/Z2M matching
- [x] Canonical `<Area> <Location> <Use>` name derivation, with overrides
- [x] Home Assistant registry operations (rename, disable, area, entity id)
- [x] Token-aware entity_id reference rewriting, with per-run backups
- [x] Zigbee2MQTT device list polling and renames
- [x] Watcher loop, dry run by default, per-run rollback scripts
- [x] Wired into the shared `run.sh` supervisor as `importer()`
- [x] Settings migration from ZHA to Z2M for the Inovelli VZM31-SN

## Open

- [ ] Validate a real `--live` migration on one device, with Chris watching
- [ ] Populate `workdir/name_overrides.json` for the devices whose names do
      not derive cleanly (53 of the 96 currently eligible)
- [ ] Lovelace storage rewrites need a Home Assistant restart to take effect,
      and can be clobbered if a dashboard is edited in the UI first. Currently
      only flagged in the log; decide whether that is good enough.
- [ ] Extend the settings map in `settings.py` beyond the Inovelli VZM31-SN.
      Any other model is currently reported as unmapped rather than migrated.
- [ ] `double_tap_up_enabled` / `double_tap_down_enabled` are left unmapped.
      Z2M's `doubleTapUpToParam55` / `doubleTapDownToParam56` look related but
      configure what a double tap does, not whether it is enabled. Confirm on
      real hardware before mapping them.

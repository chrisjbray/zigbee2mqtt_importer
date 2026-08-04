# TODO

## Build

- [x] Periodic ZHA device snapshot, with the already-migrated exclusion filter
- [x] IEEE normalisation and ZHA/Z2M matching
- [x] Canonical `[Area] [Location] [Use]` name derivation, with overrides
- [x] Home Assistant registry operations (rename, disable, area, entity id)
- [x] Token-aware entity_id reference rewriting, with per-run backups
- [x] Zigbee2MQTT device list polling and renames
- [x] Watcher loop, dry run by default, per-run rollback scripts
- [x] Wired into the shared `run.sh` supervisor as `importer()`

## Open

- [ ] Validate a real `--live` migration on one device, with Chris watching
- [ ] Populate `workdir/name_overrides.json` for the devices whose names do
      not derive cleanly (53 of the 96 currently eligible)
- [ ] Lovelace storage rewrites need a Home Assistant restart to take effect,
      and can be clobbered if a dashboard is edited in the UI first. Currently
      only flagged in the log; decide whether that is good enough.
- [ ] Intentionally out of scope: syncing ZHA device settings (reporting
      configuration, binds) into Zigbee2MQTT. Dropped during scoping, recorded
      here in case it is ever revisited.

# Decky plugin installer integration test

Offline `--root` simulation of the `--decky` component in
`tools/oxp3-hw-install.sh`: it installs the repository's deterministic plugin
zip into a scratch prefix, then exercises the paths that a real install cannot
safely rehearse on the host.

```sh
tests/integration/decky/run.sh
tools/repo-test.sh --only decky-install
```

Covered:

* install into `<prefix><user home>/homebrew/plugins/oxp3-status`, with the
  manifest and ownership marker checked;
* the read-only `--status` row (`state=ok`, version, marker);
* reinstall and the backup in
  `<prefix><home>/.local/state/oxp3-fixes/decky-backups/<stamp>/`;
* uninstall removing exactly that directory and keeping the backup;
* a missing artifact and a tampered zip (one byte appended) being refused;
* a foreign directory with the same name keeping its files when both uninstall
  and install are attempted;
* `--dry-run --all` planning the component without building or writing.

It never runs `sudo`, never restarts `plugin_loader.service` and never writes
outside the temporary prefix. Without a built `build/oxp3-status-*.zip` it exits
77 (`SKIP`) with the exact build command; it does not build the plugin itself.

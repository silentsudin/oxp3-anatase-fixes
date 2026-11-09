# Volume daemon integration test

End-to-end test for `tools/oxp3-voltap`: it creates ephemeral uinput devices,
runs the **real** daemon process against them, injects tap / three-tap / hold /
bounce / storm / orphan / late-release sequences, and reads the result from the
daemon's own uinput shim.

```sh
tests/integration/volume/run.sh
tools/repo-test.sh --integration
OXP3_REPO_TEST_INTEGRATION=1 tools/repo-test.sh --only voltap-integration
make test-integration
```

It is opt-in: the repository runner reports it as `SKIP` unless
`OXP3_REPO_TEST_INTEGRATION=1`. It needs write access to `/dev/uinput` and read
access to the event nodes it creates. Root always works; as a user, `/dev/uinput`
must be user-writable (udev ACL) and the created event nodes readable (input
group or logind uaccess ACL). When that is missing it exits 77 with the exact
enable command. It never
opens a real input device, never runs `sudo`, and tears down every virtual
device and daemon in a `finally` block.

The HHD-shaped stand-in reuses hhd's device *name string* only, with our own
vendor/product (`1209:7657`) and a `-integration-test` `phys`; the automatic
topology phase passes synthetic `--proc-root`/`--sys-root` trees so the host's
real devices are invisible. See `docs/hw/volume-buttons.md` for the proven
scenarios and `docs/VALIDATION.md` for where it sits next to the hardware
harness.

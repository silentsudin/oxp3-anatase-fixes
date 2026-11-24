# RFC draft — USB: full-speed device rejected with *Secondary Bandwidth Error* behind any hub on the Panther Lake xHCI (Steam Controller puck)

Status: **draft, not submitted**. Prepared 2026-11-21 for linux-usb (xHCI: Mathias Nyman; USB core:
Greg Kroah-Hartman) and for Anatase (`anatase-org/patchwork`), with a question for Intel. The local
workaround is a livepatch module, because usbcore and xhci-hcd are built into the Anatase kernel:
[`0014`](../patches/kernel/0014-usb-oxp3-livepatch-tt-interval-quirk.patch) (interval relaxation,
**failed on hardware**, see §7) and [`0016`](../patches/kernel/0016-usb-oxp3-livepatch-trim-unused-interfaces.patch)
(configure unused interfaces without endpoints). The upstream
change proposed in §6 is a source patch to USB core. It is **not compiled**; it is written against
the 7.3.4 tree below.

Platform: OneXPlayer 3, Intel Core Ultra (Series 3) / Panther Lake, kernel `7.3.4-an02.fc45.x86_64`.
Host: **PCH xHCI `8086:e47d` rev 01** (`0000:00:14.0`, bus 3, HCI 1.2). Hub: JSAUX RGB dock, VIA
`2109:2822` USB 2.0 hub (multi-TT, alt setting 1 active), internal Huasheng `214b:7260` single-TT
hubs. Device: **Valve Steam Controller Puck `28de:1304`** (bcdDevice 0.02), full speed.

---

## 1. Symptom

```
usb 3-1.3.2: new full-speed USB device number 101 using xhci_hcd
usb 3-1.3.2: Product: Steam Controller Puck
usb 3-1.3.2: Not enough bandwidth for new device state.
usb 3-1.3.2: can't set config #1, error -28
```

The device stays in the ADDRESS state with no interfaces. It happens behind **every** hub port on
the dock:
- the multi-TT VIA hub's own port (`3-1.2`), with the puck alone on its TT;
- either single-TT downstream hub (`3-1.3.x`, `3-1.4.x`).

The same puck works on a root port of this host. The same puck behind the same dock works on an AMD
host (Legion Go 2). So this is about this host's handling of the TT, not the dock or the puck.

## 2. Device

One configuration, seven interfaces, all alt setting 0. Periodic endpoints:

| interface | class | endpoints |
|---|---|---|
| 0 | CDC ACM | interrupt IN 16 B, bInterval 10 |
| 1 | CDC data | bulk IN/OUT 64 B |
| 2-5 | HID (one per controller slot) | interrupt IN 64 B **2 ms**, interrupt OUT 64 B **2 ms** |
| 6 | HID (management) | interrupt IN/OUT 64 B, bInterval 32 |

That is 8 × 64 B every 2 ms, 2 × 64 B every 32 ms and 16 B every 10 ms. By the USB 2.0 §5.11.3
full-speed transaction-time formula a 64-byte interrupt transaction is ≈ 15.6 µs. Even with all eight
2 ms endpoints in the same frame, that is ≈ 125 µs of the 900 µs periodic budget per TT frame.

## 3. What Linux programs (xHCI tracepoints)

`xhci_add_endpoint` for the puck (slot 40, TT hub slot 35 port 2, TT think time 0):

```
State disabled mult 1 max P. Streams 0 interval 8000 us  max ESIT payload 16 CErr 3 Type Int IN  burst 0 maxp 16 avg trb len 16
State disabled mult 1 max P. Streams 0 interval 125 us   max ESIT payload 0  CErr 3 Type Bulk IN  burst 0 maxp 64 avg trb len 0
State disabled mult 1 max P. Streams 0 interval 125 us   max ESIT payload 0  CErr 3 Type Bulk OUT burst 0 maxp 64 avg trb len 0
State disabled mult 1 max P. Streams 0 interval 2000 us  max ESIT payload 64 CErr 3 Type Int IN  burst 0 maxp 64 avg trb len 64   (x4)
State disabled mult 1 max P. Streams 0 interval 2000 us  max ESIT payload 64 CErr 3 Type Int OUT burst 0 maxp 64 avg trb len 64   (x4)
State disabled mult 1 max P. Streams 0 interval 32000 us max ESIT payload 64 CErr 3 Type Int IN  burst 0 maxp 64 avg trb len 64
State disabled mult 1 max P. Streams 0 interval 32000 us max ESIT payload 64 CErr 3 Type Int OUT burst 0 maxp 64 avg trb len 64
```

The intervals, Max ESIT Payload, Average TRB Length and TT fields are all per spec.
(`xhci_parse_frame_interval()`: bInterval 2 → exponent 4 → 16 µframes = 2 ms.)

The host's answer:

```
Configure Endpoint Command: ctx … slot 40 flags d:c
  -> Command Completion Event status 'Secondary Bandwidth Error'
```

The puck's bootloader (`Steam Controller Bootloader`, 3 endpoints) enumerates first on the same TT
port. Its slot is torn down cleanly before the puck appears: final Configure Endpoint, then
`Disable Slot` → Success. So no stale reservation is involved.

## 4. Analysis

- *Secondary Bandwidth Error* means the host's TT (secondary bandwidth domain) budget check failed.
  The load in §2 is far below the USB 2.0 TT budget, so the PTL PCH xHCI models TT bandwidth much
  more conservatively than the spec arithmetic and than AMD hosts. One plausible cause is a fixed
  per-frame allocation for every periodic split endpoint regardless of payload.
- Linux can't tell what the host will accept: there is no query, and the only feedback is the
  Configure Endpoint failure.
- **Bandwidth is not the limit.** With the intervals raised to OUT 32 ms / IN 8 ms (≈ 1/10 of the
  periodic load) the host still answers *Secondary Bandwidth Error* (§7). What it counts appears to
  be the **number of periodic endpoints** behind the TT: the puck has eleven (CDC notification,
  4 slots × IN/OUT, pogo-pin IN/OUT).
- Only one controller slot is needed per paired controller. hid-steam documents the interfaces:
  0-1 internal comms, 2-5 controller slots, 6 pogo-pin interface.

## 5. Questions for Intel

1. What is the TT (secondary bandwidth domain) periodic budget model of the PTL PCH xHCI
   `8086:e47d`? Is there a maximum number of periodic split endpoints per TT (or per device behind a
   TT), independent of their interval? 11 endpoints are rejected even at 8-32 ms intervals.
2. Is *Secondary Bandwidth Error* for this endpoint set expected, or a known erratum?
3. Is there a documented software-visible way to know the remaining TT budget?

## 6. Proposed upstream change

> **Superseded by the hardware result in §7:** interval relaxation does not get the puck accepted.
> The sketch below is kept for the record; the change to propose is the interface trim described
> after it, pending the level the host accepts.

USB core: when the host rejects a new configuration with `-ENOSPC`, and the device is full/low
speed **behind a TT**, retry with interrupt OUT intervals raised, then IN as well. The first
accepted set is kept and a single `dev_info` records it. Devices the host accepts, and devices on a
root port, are untouched.

Two variants are offered for the maintainers to choose from:
- **(a) device quirk**: only devices flagged with a new `USB_QUIRK_TT_RELAX_INTR_INTERVAL`
  (Steam Controller puck `28de:1304`) are retried. Minimal blast radius.
- **(b) generic**: every full/low-speed device behind a TT. It helps other multi-endpoint
  dongles, but it changes behaviour for devices that today fail loudly.

Sketch of (a), against `drivers/usb/core/hcd.c` (7.3.4):

```diff
--- a/include/linux/usb/quirks.h
+++ b/include/linux/usb/quirks.h
@@
+/* device behind a TT: on host bandwidth rejection, retry with longer
+ * interrupt intervals (OUT first, then IN) */
+#define USB_QUIRK_TT_RELAX_INTR_INTERVAL	BIT(19)
--- a/drivers/usb/core/quirks.c
+++ b/drivers/usb/core/quirks.c
@@ static const struct usb_device_id usb_quirk_list[] = {
+	/* Valve Steam Controller puck: 8 x 64 B interrupt at 2 ms is rejected by
+	 * Panther Lake xHCI TT budget (Secondary Bandwidth Error) behind hubs */
+	{ USB_DEVICE(0x28de, 0x1304), .driver_info = USB_QUIRK_TT_RELAX_INTR_INTERVAL },
--- a/drivers/usb/core/hcd.c
+++ b/drivers/usb/core/hcd.c
@@
-int usb_hcd_alloc_bandwidth(struct usb_device *udev,
+static int __usb_hcd_alloc_bandwidth(struct usb_device *udev,
 		struct usb_host_config *new_config,
 		struct usb_host_interface *cur_alt,
 		struct usb_host_interface *new_alt)
 { /* existing body, unchanged */ }
+
+static const struct { u8 out_ms, in_ms; } tt_relax_steps[] = {
+	{ 8, 0 }, { 16, 4 }, { 32, 8 },
+};
+
+static void tt_relax_alt(struct usb_host_interface *alt, unsigned int step)
+{
+	int j;
+
+	for (j = 0; j < alt->desc.bNumEndpoints; j++) {
+		struct usb_endpoint_descriptor *d = &alt->endpoint[j].desc;
+		u8 floor = usb_endpoint_dir_out(d) ? tt_relax_steps[step].out_ms :
+						     tt_relax_steps[step].in_ms;
+
+		if (usb_endpoint_xfer_int(d) && floor && d->bInterval < floor)
+			d->bInterval = floor;	/* FS/LS: frames */
+	}
+}
+
+int usb_hcd_alloc_bandwidth(struct usb_device *udev,
+		struct usb_host_config *new_config,
+		struct usb_host_interface *cur_alt,
+		struct usb_host_interface *new_alt)
+{
+	int ret = __usb_hcd_alloc_bandwidth(udev, new_config, cur_alt, new_alt);
+	unsigned int step, i;
+
+	if (ret != -ENOSPC || !udev->tt || !(new_config || new_alt) ||
+	    !(udev->quirks & USB_QUIRK_TT_RELAX_INTR_INTERVAL))
+		return ret;
+
+	for (step = 0; step < ARRAY_SIZE(tt_relax_steps) && ret == -ENOSPC; step++) {
+		if (new_config)
+			for (i = 0; i < new_config->desc.bNumInterfaces; i++) {
+				struct usb_host_interface *first =
+					&new_config->intf_cache[i]->altsetting[0];
+				struct usb_host_interface *alt = usb_find_alt_setting(new_config,
+						first->desc.bInterfaceNumber, 0);
+
+				tt_relax_alt(alt ? alt : first, step);
+			}
+		if (new_alt)
+			tt_relax_alt(new_alt, step);
+		ret = __usb_hcd_alloc_bandwidth(udev, new_config, cur_alt, new_alt);
+	}
+	if (!ret)
+		dev_info(&udev->dev, "TT bandwidth rejected; interrupt OUT >= %u ms, IN >= %u ms\n",
+			 tt_relax_steps[step - 1].out_ms, tt_relax_steps[step - 1].in_ms);
+	return ret;
+}
 EXPORT_SYMBOL_GPL(usb_hcd_alloc_bandwidth);
```

**Interface trim (0016).** Same hook and scope, but on `-ENOSPC` the interfaces in a per-device
trim list are configured with `bNumEndpoints = 0` in the cached altsetting, one more per retry
(puck: slots 4, 3, 2, then the pogo-pin interface, then the CDC notification endpoint). Usbcore then
creates those interfaces without endpoints, their drivers fail to probe, and the rest of the device
works; one controller in slot 1 keeps working. If the host still refuses, the descriptors are
restored. Upstream this would be a quirk with a per-device trim list, or better a fix in the
host's TT accounting once Intel answers §5.

Notes for review (interval sketch):
- **Descriptors:** the descriptor's `bInterval` is rewritten in the cached config, so
  `usb_fill_int_urb()` users (usbhid) poll at the interval the host was programmed with. This is the
  same approach as the existing `bInterval` fix-ups in `usb_parse_endpoint()`.
- **Retries:** `__usb_hcd_alloc_bandwidth()` drops and re-adds endpoints and calls
  `reset_bandwidth()` on failure, so a retry starts from a clean state.
- **Scope:** no xHCI-specific code. EHCI/OHCI schedulers never return `-ENOSPC` for this load, so
  they never reach the retry.
- **Bit number:** to be taken from the next free bit at submission time.

## 7. Evidence and test plan

- **Trace capture:** [`tools/usb/xhci-trace.sh`](../tools/usb/xhci-trace.sh): a 40 s `xhci-hcd` tracepoint + dynamic-debug
  capture around a replug. Excerpts are in §3, from 2026-11-21 22:04 on the device.
- **Interval relaxation (0014), 2026-11-21 22:19, puck on `3-1.3.2`:** every level was rejected:

  ```
  oxp3_usb_tt_quirk: active for 28de:1304
  usb 3-1.3.2: Product: Steam Controller Puck
  usb 3-1.3.2: Not enough bandwidth for new device state.        (x4: original + 3 levels)
  usb 3-1.3.2: host rejected the bandwidth behind its TT even with relaxed intervals (-28)
  usb 3-1.3.2: can't set config #1, error -28
  ```

  The same failure also happened earlier on `3-1.2`, the VIA hub's own port, where the puck was
  alone on its TT. The dock's RGB controller (`306f:1234`, one 64-byte **isochronous** IN endpoint
  every 1 ms) sits behind the same single-TT hub as `3-1.3.x`, but not behind `3-1.2`.
- **Interface trim (0016), 2026-11-21 22:48, puck on `3-1.3.2`:** the **first** trim level was
  accepted, and the controller works:

  ```
  usb 3-1.3.2: host rejected the full configuration behind its TT; configured without the endpoints of interface(s) 5
  ```

  So the host accepts **10** periodic endpoints behind this TT (CDC notification + slots 1-3 +
  pogo pins) and rejects **11**. The interval of those endpoints doesn't matter (see above). That
  is the number for §5.
- **Regression:** the puck on a root port must keep 2 ms (no message), and other dock devices must
  be unchanged.

## 8. Open points

- ~~Which trim level the PTL host accepts~~: the first one (10 endpoints accepted, 11 rejected; §7).
- Intel's answer to §5. If the host's model is a bug, an xHCI quirk for `8086:e47d` (or a firmware
  fix) is better than relaxing devices.
- Whether Valve can ship puck firmware with fewer or slower periodic endpoints, e.g. 4 ms OUT.

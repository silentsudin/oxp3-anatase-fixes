#!/usr/bin/env python3
"""usb_probe - does this BF63112 fingerprint module attach over USB instead of SPI?

The ONEXPLAYER 3's fingerprint module is wired to the Intel LPSS SPI bus on this
board, but the vendor's UMDF driver ships a complete second transport for a
USB-wired variant of the same module: the INF binds `USB\\VID_347D&PID_0302` /
`PID_0304` through WinUSB, and the module firmware carries a vendor-class USB
descriptor with bulk endpoints.  See docs/hw/fingerprint-transport.md for the
reverse-engineering evidence and the per-SKU verdict.

This tool answers exactly one question - "is a 347D device present, and if so
what does it expose?" - and nothing else:

  * it enumerates and describes; it never opens the device for I/O,
  * it never claims an interface, never issues a control/bulk transfer and
    never sends a vendor command,
  * it works without pyusb (sysfs fallback), so it runs on a stock host.

Exit status: 0 = found, 2 = no 347D device, 3 = no USB view at all.

Usage:
    ./usb_probe.py                 # human readable
    ./usb_probe.py --json          # machine readable
    ./usb_probe.py --all-vendors   # also list every vendor-class interface

If it reports a hit, the transport decision flips: claim interface 0 with
libusb, pick the bulk IN/OUT pair from the descriptor, and drive the existing
frame library (tools/fingerprint/fpbf63112/) over bulk transfers.
"""

import argparse
import json
import os
import sys

USB_VID = 0x347D
KNOWN_PIDS = {
    0x0302: "BF63112 USB variant (full-speed 64 B / high-speed 512 B bulk)",
    0x0304: "BF63112 USB variant (second firmware flavour)",
}

SYSFS_USB = "/sys/bus/usb/devices"
VENDOR_CLASS = 0xFF


class UsbError(RuntimeError):
    pass


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def _sysfs_interface(ifdir):
    """Describe one interface node (e.g. 3-4:1.0) from sysfs only."""
    base = os.path.join(SYSFS_USB, ifdir)
    return {
        "interface": ifdir,
        "class": _read(os.path.join(base, "bInterfaceClass")),
        "subclass": _read(os.path.join(base, "bInterfaceSubClass")),
        "protocol": _read(os.path.join(base, "bInterfaceProtocol")),
        "num_endpoints": _read(os.path.join(base, "bNumEndpoints")),
        "driver": os.path.basename(os.path.realpath(os.path.join(base, "driver")))
        if os.path.exists(os.path.join(base, "driver"))
        else None,
    }


def _sysfs_endpoints(ifdir):
    """Endpoint addresses/attributes from the interface node's children."""
    out = []
    base = os.path.join(SYSFS_USB, ifdir)
    for iface in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        # ep_81 style directories live below the interface
        if not iface.startswith("ep_"):
            continue
        ep = os.path.join(base, iface)
        out.append({
            "endpoint": _read(os.path.join(ep, "bEndpointAddress")),
            "attributes": _read(os.path.join(ep, "bmAttributes")),
            "max_packet": _read(os.path.join(ep, "wMaxPacketSize")),
        })
    return out


def scan_sysfs(include_all_vendors=False):
    """Walk /sys/bus/usb/devices. Returns (hits, vendor_class_interfaces, buses)."""
    if not os.path.isdir(SYSFS_USB):
        raise UsbError("no %s (no USB stack on this host?)" % SYSFS_USB)

    hits, vendor_ifaces, buses = [], [], []
    for name in sorted(os.listdir(SYSFS_USB)):
        devdir = os.path.join(SYSFS_USB, name)
        if not os.path.isdir(devdir):
            continue
        if name.startswith("usb"):
            # root hub: used for the port-occupancy report
            buses.append({
                "device": name,
                "busnum": _read(os.path.join(devdir, "busnum")),
                "maxchild": _read(os.path.join(devdir, "maxchild")),
                "speed": _read(os.path.join(devdir, "speed")),
            })
            continue
        vid = _read(os.path.join(devdir, "idVendor"))
        pid = _read(os.path.join(devdir, "idProduct"))
        if not vid or ":" in name:
            continue

        ifaces = [
            i for i in sorted(os.listdir(devdir))
            if ":" in i and os.path.isdir(os.path.join(SYSFS_USB, i))
        ]
        if include_all_vendors:
            for i in ifaces:
                info = _sysfs_interface(i)
                if info["class"].lower() == "ff":
                    info.update({"vid": vid, "pid": pid, "device": name})
                    vendor_ifaces.append(info)

        if int(vid, 16) != USB_VID:
            continue

        info = {
            "device": name,
            "vid": vid,
            "pid": pid,
            "pid_known": KNOWN_PIDS.get(int(pid, 16)),
            "busnum": _read(os.path.join(devdir, "busnum")),
            "devnum": _read(os.path.join(devdir, "devnum")),
            "speed": _read(os.path.join(devdir, "speed")),
            "manufacturer": _read(os.path.join(devdir, "manufacturer")),
            "product": _read(os.path.join(devdir, "product")),
            "serial": _read(os.path.join(devdir, "serial")),
            "num_interfaces": len(ifaces),
            "interfaces": [],
        }
        for i in ifaces:
            entry = _sysfs_interface(i)
            entry["endpoints"] = _sysfs_endpoints(i)
            info["interfaces"].append(entry)
        hits.append(info)
    return hits, vendor_ifaces, buses


def scan_pyusb():
    """Optional richer view through pyusb (still read-only: no I/O)."""
    import usb.core  # noqa: F401  (imported lazily on purpose)

    found = []
    for dev in usb.core.find(find_all=True, idVendor=USB_VID):
        entry = {
            "vid": "0x%04x" % dev.idVendor,
            "pid": "0x%04x" % dev.idProduct,
            "pid_known": KNOWN_PIDS.get(dev.idProduct),
            "busnum": dev.bus,
            "devnum": dev.address,
            "speed": getattr(dev, "speed", None),
            "manufacturer": dev.manufacturer,
            "product": dev.product,
            "serial": dev.serial_number,
            "interfaces": [],
            "source": "pyusb",
        }
        sys_dir = getattr(dev, "sys_path", None)
        for cfg in dev:
            for intf in cfg:
                iface = {
                    "interface": intf.bInterfaceNumber,
                    "class": intf.bInterfaceClass,
                    "subclass": intf.bInterfaceSubClass,
                    "protocol": intf.bInterfaceProtocol,
                    "num_endpoints": intf.bNumEndpoints,
                    "driver": None,
                    "endpoints": [
                        {
                            "endpoint": "0x%02x" % ep.bEndpointAddress,
                            "attributes": "0x%02x" % ep.bmAttributes,
                            "max_packet": ep.wMaxPacketSize,
                        }
                        for ep in intf
                    ],
                }
                if sys_dir:
                    drv = os.path.join(
                        sys_dir, "%d.%d" % (cfg.bConfigurationValue, intf.bInterfaceNumber),
                        "driver",
                    )
                    if os.path.exists(drv):
                        iface["driver"] = os.path.basename(os.path.realpath(drv))
                entry["interfaces"].append(iface)
        found.append(entry)
    return found


def collect(include_all_vendors):
    """Try pyusb first (gives endpoints even on old kernels), then sysfs."""
    mode, hits, vendor_ifaces, buses = "sysfs", [], [], []
    try:
        hits = scan_pyusb()
        mode = "pyusb"
    except ImportError:
        mode = "sysfs"
    except Exception as exc:  # pyusb present but the backend failed
        mode = "sysfs (pyusb unusable: %s)" % exc
    # sysfs is still the authority for "is it there at all" and for the
    # occupancy report; it also covers the no-pyusb case.
    sys_hits, vendor_ifaces, buses = scan_sysfs(include_all_vendors)
    if not hits:
        hits = sys_hits
    else:
        for h in hits:
            h.setdefault("source", "pyusb")
    return mode, hits, vendor_ifaces, buses


def report_human(mode, hits, vendor_ifaces, buses):
    print("BF63112 USB probe - read-only, no vendor command is sent")
    print("enumeration: %s" % mode)
    print()
    if hits:
        print("RESULT: %d x USB\\VID_%04X device(s) present" % (len(hits), USB_VID))
        for h in hits:
            print("  %s:%s  bus %s dev %s  speed %s"
                  % (h["vid"], h["pid"], h.get("busnum"), h.get("devnum"), h.get("speed")))
            if h.get("pid_known"):
                print("    known pid: %s" % h["pid_known"])
            for key in ("manufacturer", "product", "serial"):
                if h.get(key):
                    print("    %-12s %s" % (key + ":", h[key]))
            for i in h["interfaces"]:
                print("    if %s  class %s/%s/%s  endpoints %s  driver %s"
                      % (i.get("interface"), i.get("class"), i.get("subclass"),
                         i.get("protocol"), i.get("num_endpoints"), i.get("driver")))
                for ep in i.get("endpoints", []):
                    print("      ep %s  attr %s  mps %s"
                          % (ep.get("endpoint"), ep.get("attributes"), ep.get("max_packet")))
        print()
        print("A hit means this unit is USB-wired: libusb/libusb backend is viable,")
        print("the SPI/ACPI path is not needed. See docs/hw/fingerprint-transport.md.")
        return 0

    print("RESULT: no USB\\VID_%04X device on this host." % USB_VID)
    print("        The sensor is on the SPI/ACPI path (see fingerprint-transport.md).")
    print()
    if buses:
        print("USB topology (root hubs, ports: max child / used):")
        for b in buses:
            used = 0
            dev = b["device"]
            prefix = dev.replace("usb", "")
            for name in sorted(os.listdir(SYSFS_USB)):
                if name.startswith(prefix + "-") and name.count(".") == 0:
                    used += 1
            print("  %-6s bus %-3s ports %-3s used %s"
                  % (dev, b.get("busnum"), b.get("maxchild"), used))
    if vendor_ifaces:
        print()
        print("Other vendor-class (0xFF) interfaces, for comparison - a BF63112")
        print("USB module would show up here with two BULK endpoints:")
        for i in vendor_ifaces:
            print("  %s:%s if %s class ff/%s/%s endpoints %s driver %s"
                  % (i["vid"], i["pid"], i["interface"], i["subclass"],
                     i["protocol"], i["num_endpoints"], i["driver"]))
    print()
    print("Note: absence of 347D is expected on this board and is not a fault.")
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Read-only enumeration probe for the BF63112 USB variant (VID 347D).",
    )
    ap.add_argument("--json", action="store_true", help="machine readable output")
    ap.add_argument("--all-vendors", action="store_true",
                    help="also list every vendor-class interface on the bus")
    args = ap.parse_args(argv)

    try:
        mode, hits, vendor_ifaces, buses = collect(args.all_vendors)
    except UsbError as exc:
        if args.json:
            print(json.dumps({"error": str(exc), "found": False, "exit": 3}))
        else:
            print("usb_probe: %s" % exc, file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps({
            "vid": "0x%04x" % USB_VID,
            "enumeration": mode,
            "found": bool(hits),
            "devices": hits,
            "vendor_class_interfaces": vendor_ifaces,
            "root_hubs": buses,
            "exit": 0 if hits else 2,
        }, indent=2, sort_keys=True))
        return 0 if hits else 2

    return report_human(mode, hits, vendor_ifaces, buses)


if __name__ == "__main__":
    sys.exit(main())

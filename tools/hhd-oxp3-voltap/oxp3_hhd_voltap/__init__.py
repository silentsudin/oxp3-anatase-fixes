# SPDX-License-Identifier: MIT
"""oxp3-hhd-voltap - volume rocker tap normalisation as an HHD plugin.

Registered in the ``hhd.plugins`` entry-point group.  HHD's contract is::

    hhd/__main__.py  ->  entry_points(group="hhd.plugins")
                         ep.load()                      # our callable
                         ep_value([])                   # -> Sequence[HHDPlugin]
                         sorted_plugins += result
                         plugin.open(emit, context)     # once
                         plugin.settings()              # on config reload
                         plugin.validate(...)           # config values
                         plugin.prepare(conf)           # every tick, reverse order
                         plugin.update(conf)            # every tick
                         plugin.notify(events)          # every tick, control events
                         plugin.close()                 # shutdown

There is no per-event input callback in that contract, so this plugin does the
normalisation where HHD itself handles the OXP3 rocker: a defensive wrapper
around ``hhd.device.oxp.base.OxpAtKbd.produce`` (see :mod:`hhd_hook`).  It
never opens, reads or grabs an event device - which is the whole point: the
previous daemon took an ``EVIOCGRAB`` on hhd's own virtual "Handheld Daemon
Volume Keyboard" and hhd's OXP keyboard producer then failed with EBUSY, which
made hhd assume the controller had disconnected.

Failure policy: every step is wrapped.  Import problems, a changed HHD device
class, a changed method signature or an exception inside the state machine all
log and no-op (or restore HHD's own handling) instead of breaking HHD.

Tuning (environment, read at plugin load)::

    OXP3_HHD_VOLTAP=0             disable the plugin without uninstalling
    OXP3_HHD_VOLTAP_HOLD_MS=60    minimum synthetic hold per tap
    OXP3_HHD_VOLTAP_GAP_MS=60     bounce window after a tap
    OXP3_HHD_VOLTAP_MAX_HOLD_MS=500  cap for any synthetic hold

Author: silentsudin <25103587+silentsudin@users.noreply.github.com>
Licence: GPL-2.0-or-later - our own code, no vendor code involved.
"""

from __future__ import annotations

import logging
import os

from . import hhd_hook
from .tap import DEFAULT_GAP, DEFAULT_HOLD, DEFAULT_MAX_HOLD, TapNormaliser

__version__ = "0.1.0"

logger = logging.getLogger(__name__)

#: HHD imports the entry point *outside* its per-plugin error handling.  Keep
#: this import from ever raising, even on a host where hhd is half-installed.
try:  # pragma: no cover - depends on the host
    from hhd.plugins import HHDPlugin as _HHDPluginBase
except Exception:  # pragma: no cover - depends on the host
    _HHDPluginBase = object


def _enabled() -> bool:
    return os.environ.get("OXP3_HHD_VOLTAP", "1").strip().lower() not in (
        "0", "no", "off", "false", "",
    )


def _env_seconds(name: str, default_ms: float) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default_ms / 1000.0
    try:
        return max(0.0, float(raw)) / 1000.0
    except (TypeError, ValueError):
        logger.warning("oxp3-voltap: ignoring invalid %s=%r", name, raw)
        return default_ms / 1000.0


class VolumeTapPlugin(_HHDPluginBase):
    """An HHD plugin that normalises volume taps and manages no device."""

    def __init__(self):
        self.name = "oxp3_voltap"
        # Device plugins (oxp/generic/...) use priorities in the 0-100 range and
        # are sorted ascending; any value works here because we never provide
        # settings or config.  Keep it high so we run last and never delay a
        # device plugin's start.
        self.priority = 90
        self.log = "vltp"
        self.emit = None
        self.context = None
        self.hook = {"installed": False, "reason": "not opened"}
        self.hold = _env_seconds("OXP3_HHD_VOLTAP_HOLD_MS", DEFAULT_HOLD * 1000)
        self.gap = _env_seconds("OXP3_HHD_VOLTAP_GAP_MS", DEFAULT_GAP * 1000)
        self.max_hold = _env_seconds("OXP3_HHD_VOLTAP_MAX_HOLD_MS",
                                     DEFAULT_MAX_HOLD * 1000)

    # -- HHD lifecycle -----------------------------------------------------
    def open(self, emit, context):  # noqa: A003 - name fixed by HHD
        """Called once by hhd.  Must never raise."""
        self.emit = emit
        self.context = context
        try:
            if not _enabled():
                self.hook = {"installed": False, "reason": "disabled by env"}
                logger.info("oxp3-voltap: disabled by OXP3_HHD_VOLTAP; no-op.")
                return
            probe = hhd_hook.probe()
            if not probe.get("available"):
                self.hook = {**probe, "installed": False}
                logger.warning(
                    "oxp3-voltap: no-op, this HHD build does not expose the "
                    "OXP keyboard producer we know how to hook (%s). Volume "
                    "buttons are left exactly as HHD handles them.",
                    probe.get("reason"),
                )
                return
            self.hook = hhd_hook.install(
                hold=self.hold, gap=self.gap, max_hold=self.max_hold,
                log=logger,
            )
        except Exception:
            # A plugin error removes the plugin from hhd's list; that is fine,
            # but the controller must keep working, which it does because we
            # never touched a device.
            logger.exception(
                "oxp3-voltap: no-op after an unexpected error while installing "
                "the tap filter; HHD's own handling is unchanged."
            )
            self.hook = {"installed": False, "reason": "exception in open()"}

    def settings(self):
        # No UI surface: tuning is deliberately out of the HHD config schema so
        # a config-shape change in HHD can never break this plugin.
        return {}

    def validate(self, tags, config, value):
        return False

    def prepare(self, conf):
        # Called every tick before update(). Nothing to do: the filter lives in
        # the producer, not in config.
        return None

    def update(self, conf):
        return None

    def notify(self, events):
        # HHD delivers control-plane events here (settings/profile/acpi/...),
        # never controller input, so there is nothing to filter.
        return None

    def close(self):
        try:
            if self.hook.get("installed"):
                hhd_hook.uninstall(log=logger)
        except Exception:
            logger.exception("oxp3-voltap: could not remove the HHD hook.")

    # -- introspection (used by install.sh status/check) -------------------
    def describe(self) -> dict:
        return {
            "name": self.name,
            "priority": self.priority,
            "enabled": _enabled(),
            "hook": dict(self.hook),
            "hold_ms": round(self.hold * 1000, 1),
            "gap_ms": round(self.gap * 1000, 1),
            "max_hold_ms": round(self.max_hold * 1000, 1),
        }


def autodetect(existing=None):
    """Entry point target: HHD calls this with the plugins detected so far.

    Returns a one-element sequence, or an empty one when HHD is not importable
    or the plugin is disabled - never raises, because HHD calls this inside its
    startup path.
    """
    try:
        if not _enabled():
            logger.info("oxp3-voltap: disabled by OXP3_HHD_VOLTAP; not loaded.")
            return []
        return [VolumeTapPlugin()]
    except Exception:
        logger.exception("oxp3-voltap: could not create the plugin; ignoring.")
        return []


__all__ = ["VolumeTapPlugin", "autodetect", "TapNormaliser", "__version__"]

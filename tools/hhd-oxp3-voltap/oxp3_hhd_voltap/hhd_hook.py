# SPDX-License-Identifier: MIT
"""The one narrow hook into HHD's controller pipeline.

HHD's public plugin API (``hhd.plugins``, group ``hhd.plugins``) gives a
plugin lifecycle hooks (``open``/``settings``/``validate``/``prepare``/
``update``/``notify``/``close``) and an ``Emitter`` for *injecting* events.  It
has no per-event input filter and no way to suppress a producer event: the
controller path is ``Producer.produce() -> Multiplexer.process() -> Consumer``
inside ``hhd/device/*`` and never reaches a plugin.  ``Emitter.register_intercept``
is not a filter either - it is the overlay's whole-controller grab and hides all
buttons/axes from the consumers while active.

So the volume-tap policy has to sit where HHD itself normalises the OXP3
rocker: the AT-keyboard producer of the OXP device plugin,
``hhd.device.oxp.base.OxpAtKbd`` (``hhd/device/oxp/base.py``).  We wrap its
``produce()`` for volume buttons only and leave every other event untouched.

This shim is deliberately paranoid because it touches a private device class:

* everything is probed and imported lazily; a missing module/class/method is a
  logged no-op, never an exception on HHD's startup path;
* the wrapper never raises into HHD: on any unexpected error it logs once,
  restores HHD's own tap synthesis and passes HHD's events through unchanged;
* a failure of the state machine disables it for the rest of the boot, so it
  cannot repeatedly perturb the controller.

The supported long-term fix is upstream HHD carrying this policy itself; this
module exists so the owner can be safe on today's immutable host without any
process grabbing HHD's devices.
"""

from __future__ import annotations

import logging
import time

from .tap import (
    DEFAULT_GAP,
    DEFAULT_HOLD,
    DEFAULT_MAX_HOLD,
    VOLUME_CODES,
    TapNormaliser,
)

logger = logging.getLogger(__name__)

#: The device class that owns the OXP3 rocker inside HHD.
TARGET_MODULE = "hhd.device.oxp.base"
TARGET_CLASS = "OxpAtKbd"

_MARK = "_oxp3_voltap_wrap"
_ORIGINAL = "_oxp3_voltap_original"


class HookUnavailable(RuntimeError):
    """The HHD API shape is not what this plugin was written against."""


def _module(module_name: str = TARGET_MODULE):
    import importlib

    return importlib.import_module(module_name)


def probe(module_name: str = TARGET_MODULE, class_name: str = TARGET_CLASS) -> dict:
    """Return what this hook can see, without changing anything.

    Never raises: an import failure is reported as ``available=False`` so the
    caller (and ``install.sh check``) can print a reason instead of a traceback.
    """
    info = {
        "module": module_name,
        "class": class_name,
        "available": False,
        "reason": "",
        "has_volume_taps": False,
    }
    try:
        module = _module(module_name)
    except Exception as exc:  # pragma: no cover - environment dependent
        info["reason"] = f"cannot import {module_name}: {exc}"
        return info
    cls = getattr(module, class_name, None)
    if cls is None:
        info["reason"] = f"{module_name}.{class_name} is missing"
        return info
    if not isinstance(cls, type):
        info["reason"] = f"{module_name}.{class_name} is not a class"
        return info
    produce = getattr(cls, "produce", None)
    if not callable(produce):
        info["reason"] = f"{module_name}.{class_name}.produce is not callable"
        return info
    if getattr(cls, _MARK, False):
        info.update(available=True, reason="already wrapped (idempotent)")
        info["installed"] = True
        return info
    try:
        import inspect

        params = list(inspect.signature(produce).parameters)
    except Exception:
        params = []
    if params and params[0] != "self":
        info["reason"] = (
            f"{module_name}.{class_name}.produce has an unexpected signature "
            f"{params}; refusing to wrap"
        )
        return info
    init = getattr(cls, "__init__", None)
    code = getattr(init, "__code__", None)
    info.update(
        available=True,
        reason="ok",
        has_volume_taps=bool(code is not None and "volume_taps" in code.co_names),
    )
    return info


def apply_filter(
    normaliser: TapNormaliser,
    events,
    now: float | None = None,
    volume_codes=VOLUME_CODES,
):
    """Normalise volume button events and append any due releases.

    Pure apart from ``normaliser`` state, so the unit suite can exercise it
    without HHD or a device.  Emits HHD-shaped ``button`` events
    (``{"type": "button", "code": ..., "value": bool}``) and passes every other
    event object through object-identically.
    """
    if now is None:
        now = time.perf_counter()
    out = []
    for ev in events:
        try:
            is_button = (
                isinstance(ev, dict)
                and ev.get("type") == "button"
                and ev.get("code") in volume_codes
            )
        except Exception:
            is_button = False
        if not is_button:
            out.append(ev)
            continue
        for code, value in normaliser.guard(
            normaliser.handle, now, ev.get("code"), ev.get("value")
        ):
            out.append({"type": "button", "code": code, "value": value})
    for code, value in normaliser.guard(normaliser.poll, now):
        out.append({"type": "button", "code": code, "value": value})
    return out


def _normaliser(self, hold, gap, max_hold):
    norm = getattr(self, "_oxp3_voltap_norm", None)
    if norm is None:
        norm = TapNormaliser(hold=hold, gap=gap, max_hold=max_hold)
        try:
            self._oxp3_voltap_norm = norm
        except Exception:
            # An exotic class with __slots__ must not break the producer.
            pass
    return norm


def install(
    module_name: str = TARGET_MODULE,
    class_name: str = TARGET_CLASS,
    *,
    hold: float = DEFAULT_HOLD,
    gap: float = DEFAULT_GAP,
    max_hold: float = DEFAULT_MAX_HOLD,
    log: logging.Logger | None = None,
) -> dict:
    """Wrap ``OxpAtKbd.produce``.  Returns a status dict; never raises."""
    log = log or logger
    info = probe(module_name, class_name)
    if not info.get("available"):
        log.warning("oxp3-voltap: no-op, HHD hook unavailable: %s",
                    info.get("reason"))
        return {"installed": False, "reason": info.get("reason"),
                "available": False}
    if info.get("installed"):
        log.info("oxp3-voltap: HHD hook already installed.")
        return {"installed": True, "reason": "already wrapped",
                "available": True}

    try:
        module = _module(module_name)
        cls = getattr(module, class_name)
        original = cls.produce
    except Exception as exc:  # pragma: no cover - race with HHD import
        log.warning("oxp3-voltap: no-op, could not resolve the HHD hook: %s",
                    exc)
        return {"installed": False, "reason": str(exc), "available": False}

    def produce(self, *args, **kwargs):  # noqa: ANN001 - HHD calls this
        # A failed filter stays failed for the boot: HHD's own tap handling is
        # back on, so do not touch the flag again and do not reprocess events.
        if getattr(self, "_oxp3_voltap_failed", False):
            return original(self, *args, **kwargs)
        # HHD's own fixed 60 ms tap synthesis would double-normalise; switch it
        # off for this instance and take over (restored if we fail).
        disabled_own = False
        if getattr(self, "volume_taps", False):
            try:
                self.volume_taps = False
                disabled_own = True
            except Exception:
                pass
        try:
            events = list(original(self, *args, **kwargs))
        except Exception:
            # A producer error means "controller disconnected" to HHD; never
            # swallow or rewrite it.
            raise
        try:
            norm = _normaliser(self, hold, gap, max_hold)
            return apply_filter(norm, events)
        except Exception as exc:
            log.error(
                "oxp3-voltap: tap normaliser failed (%s); passing HHD's events "
                "through unchanged and restoring HHD's own tap handling. The "
                "controller is not affected.", exc, exc_info=True,
            )
            try:
                self._oxp3_voltap_failed = True
                # Restore HHD's own tap handling regardless of which batch
                # disabled it, so a broken filter degrades to HHD's baseline.
                if hasattr(self, "volume_taps"):
                    self.volume_taps = True
            except Exception:
                pass
            return events

    try:
        cls.produce = produce
        setattr(cls, _ORIGINAL, original)
        setattr(cls, _MARK, True)
    except Exception as exc:  # pragma: no cover - exotic metaclass
        log.warning("oxp3-voltap: no-op, could not install the HHD hook: %s",
                    exc)
        return {"installed": False, "reason": str(exc), "available": True}

    log.info(
        "oxp3-voltap: installed the volume-tap filter on %s.%s.produce "
        "(hold=%.0f ms, gap=%.0f ms, max_hold=%.0f ms); no input device is "
        "opened or grabbed by this plugin.",
        module_name, class_name, hold * 1000, gap * 1000, max_hold * 1000,
    )
    return {"installed": True, "reason": "wrapped", "available": True}


def uninstall(
    module_name: str = TARGET_MODULE,
    class_name: str = TARGET_CLASS,
    *,
    log: logging.Logger | None = None,
) -> dict:
    """Undo :func:`install` if (and only if) the class is still ours."""
    log = log or logger
    try:
        module = _module()
        cls = getattr(module, class_name, None)
        if cls is None or not getattr(cls, _MARK, False):
            return {"removed": False, "reason": "not installed"}
        original = getattr(cls, _ORIGINAL, None)
        if original is None:
            return {"removed": False, "reason": "original produce is missing"}
        cls.produce = original
        try:
            delattr(cls, _MARK)
            delattr(cls, _ORIGINAL)
        except Exception:
            pass
        log.info("oxp3-voltap: removed the HHD volume-tap filter.")
        return {"removed": True, "reason": "restored"}
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("oxp3-voltap: could not remove the HHD hook: %s", exc)
        return {"removed": False, "reason": str(exc)}

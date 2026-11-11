"""Test bootstrap: stub Home Assistant modules so the coordinator is importable.

The coordinator needs only a handful of HA objects at import time; everything
runtime-related is provided by the FakeHass in test_coordinator_smoke.py.
"""

from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "custom_components" / "hydraulic_balance"


def _stub(name: str, **attrs):
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install_stubs() -> None:
    _stub("homeassistant")
    _stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    _stub(
        "homeassistant.exceptions",
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
    )
    _stub("homeassistant.helpers")

    class Store:
        def __init__(self, hass, version, key, *args, **kwargs):
            self.hass = hass
            self.key = key

        async def async_load(self):
            return getattr(self.hass, "_store_data", {}).get(self.key)

        async def async_save(self, data):
            getattr(self.hass, "_store_data", {})[self.key] = data

    _stub("homeassistant.helpers.storage", Store=Store)


def _install_packages() -> None:
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.hydraulic_balance", PKG_DIR),
    ):
        if name not in sys.modules:
            mod = types.ModuleType(name)
            mod.__path__ = [str(path)]
            sys.modules[name] = mod


_install_stubs()
_install_packages()

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "salesforce_chatter"
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


class _RegistryCtx:
    """Register the platform before adapter creation, as the gateway does."""

    def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None, required_env=None,
                          install_hint="", **entry_kwargs):
        from gateway.platform_registry import PlatformEntry, platform_registry

        platform_registry.register(PlatformEntry(
            name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
            validate_config=validate_config, required_env=required_env or [], install_hint=install_hint,
            **entry_kwargs,
        ))


@pytest.fixture
def load_plugin():
    """Load the plugin package like Hermes' _load_directory_module."""

    def _load():
        parent = "hermes_plugins"
        if parent not in sys.modules:
            ns = types.ModuleType(parent)
            ns.__path__ = []
            sys.modules[parent] = ns
        name = f"{parent}.salesforce_chatter"
        for loaded in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[loaded]
        spec = importlib.util.spec_from_file_location(
            name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = name
        module.__path__ = [str(PLUGIN_DIR)]
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.register(_RegistryCtx())
        return module

    return _load

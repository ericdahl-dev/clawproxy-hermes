"""Load the plugin as a package the way Hermes's plugin loader does.

The repo root is the plugin directory, and its name has a hyphen, so it can't be imported
directly. Register it as ``clawproxy_hermes`` with the repo root as its search location.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `from fakes import ...`
PKG = "clawproxy_hermes"

if PKG not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)


def _register_like_hermes() -> None:
    """Register through a real PlatformEntry, as Hermes's plugin context does.

    This makes Platform("clawproxy") resolvable and fails with TypeError if register() passes a
    keyword PlatformEntry doesn't accept.
    """
    from gateway.platform_registry import PlatformEntry, platform_registry

    class _Ctx:
        def register_platform(self, name, label, adapter_factory, check_fn, validate_config=None,
                              required_env=None, install_hint="", **entry_kwargs):
            entry_kwargs.setdefault("plugin_name", "clawproxy")
            platform_registry.register(PlatformEntry(
                name=name, label=label, adapter_factory=adapter_factory, check_fn=check_fn,
                validate_config=validate_config, required_env=required_env or [],
                install_hint=install_hint, source="plugin", **entry_kwargs))

    sys.modules[PKG].register(_Ctx())


_register_like_hermes()

"""Load the plugin as a package the way Hermes's plugin loader does.

The repo root is the plugin directory, and its name has a hyphen, so it can't be imported
directly. Register it as ``clawproxy_hermes`` with the repo root as its search location.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = "clawproxy_hermes"

if PKG not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)

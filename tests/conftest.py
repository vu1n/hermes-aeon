"""Load the plugin as a synthetic `hermes_aeon` package so tests can import it via absolute paths.

The plugin dir is named with a hyphen (`hermes-aeon`) which isn't a valid Python identifier,
so we register an importlib-loaded package under the underscore name.
"""
import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Ensure hermes-agent is importable for MemoryProvider / tools.registry references.
HERMES_REPO = Path("/Users/vuln/code/hermes-agent")
if HERMES_REPO.exists():
    sys.path.insert(0, str(HERMES_REPO))


def _install_package(pkg_name: str, root: Path) -> None:
    if pkg_name in sys.modules:
        return
    init_file = root / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        init_file,
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)


_install_package("hermes_aeon", PLUGIN_ROOT)

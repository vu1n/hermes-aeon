"""hermes-aeon — aeon memory provider for Hermes-Agent.

The bundled memory-provider discovery loader is shaped for flat plugins
(single ``__init__.py`` + sibling ``.py`` files) and pre-loads sibling
modules WITHOUT first registering parent namespace packages. For our
sub-package layout, that pre-load fails silently and leaves broken stubs
in ``sys.modules``. We patch around it here: register parent namespaces,
evict any broken stubs, then pre-install sub-packages cleanly.
"""
import importlib.util
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG = __name__  # "_hermes_user_memory.hermes-aeon" via discovery; "hermes_aeon" in tests


def _ensure_parent_namespaces() -> None:
    parts = _PKG.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            mod = types.ModuleType(parent)
            mod.__path__ = []
            sys.modules[parent] = mod


def _evict_broken_children() -> None:
    """Remove any partially-loaded children left by an earlier failed pre-load."""
    prefix = _PKG + "."
    for key in list(sys.modules.keys()):
        if key.startswith(prefix):
            sys.modules.pop(key, None)


def _install_subpackage(rel: str) -> None:
    full = f"{_PKG}.{rel}"
    if full in sys.modules:
        return
    sub_path = _HERE / rel.replace(".", "/")
    init = sub_path / "__init__.py"
    if not init.exists():
        return
    spec = importlib.util.spec_from_file_location(
        full, str(init), submodule_search_locations=[str(sub_path)]
    )
    if spec is None:
        return
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(full, None)


_ensure_parent_namespaces()
_evict_broken_children()
for _sub in ("store", "tone", "tools", "tools.browser_providers"):
    _install_subpackage(_sub)


def register(ctx) -> None:
    from .provider import AeonMemoryProvider
    ctx.register_memory_provider(AeonMemoryProvider())

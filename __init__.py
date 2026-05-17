"""hermes-aeon — aeon memory provider for Hermes-Agent.

A single plugin loaded by two discovery paths:

* Memory-provider discovery (``plugins/memory/__init__.py``) — flat-plugin
  loader that pre-loads sibling ``.py`` files but does not register parent
  namespace packages. We patch around it on import: register parent
  namespaces, evict broken stubs, then pre-install our sub-packages so
  ``from .store.db import ...`` etc. resolve.
* General plugin manager (``hermes_cli/plugins.py``) — also scans
  ``~/.hermes/plugins/`` and calls ``register(ctx)`` with a real
  ``PluginContext`` that exposes ``register_hook``. We register the tone
  ``pre_llm_call`` hook there.

``register(ctx)`` checks which context shape it has and calls only the
methods that exist, so the same entry point works for both paths.
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
    """Remove any partially-loaded children from an earlier failed pre-load."""
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
for _sub in ("store", "tone", "tools", "tools.browser_providers", "ingest"):
    _install_subpackage(_sub)


def register(ctx) -> None:
    """Register with whichever discovery path called us.

    Memory discovery passes a ``_ProviderCollector`` (has
    ``register_memory_provider``); the general plugin manager passes a
    ``PluginContext`` (has ``register_hook``). Branching by ``hasattr``
    lets one entry point serve both.
    """
    if hasattr(ctx, "register_memory_provider"):
        from .provider import AeonMemoryProvider
        ctx.register_memory_provider(AeonMemoryProvider())

    if hasattr(ctx, "register_hook"):
        from .tone.hook import on_pre_llm_call
        ctx.register_hook("pre_llm_call", on_pre_llm_call)

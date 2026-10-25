"""Load the checkout as the ``aeon_emerald`` package, the way Hermes loads the plugin directory."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if os.environ.get("HERMES_AGENT_PATH"):
    sys.path.insert(0, os.environ["HERMES_AGENT_PATH"])
if "aeon_emerald" not in sys.modules:
    spec = importlib.util.spec_from_file_location("aeon_emerald", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["aeon_emerald"] = module
    spec.loader.exec_module(module)

"""Load the checkout as the ``aeon_emerald`` package, the way Hermes loads the plugin directory."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# setting() prefers the process environment, so a shell with real values exported would point tests at
# the owner's Emerald, Oura and FreshRSS. Every test starts without them.
REAL_SETTINGS = ("HERMES_HOME", "AEON_EMERALD_URL", "AEON_EMERALD_KEY", "AEON_EMERALD_OURA_KEY", "OURA_PAT",
                 "FRESHRSS_URL", "FRESHRSS_USER", "FRESHRSS_PASSWORD_FILE", "FRESHRSS_PASSWORD_VAR",
                 "FRESHRSS_API_PASSWORD")


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch):
    for name in REAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)


if os.environ.get("HERMES_AGENT_PATH"):
    sys.path.insert(0, os.environ["HERMES_AGENT_PATH"])
if "aeon_emerald" not in sys.modules:
    spec = importlib.util.spec_from_file_location("aeon_emerald", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["aeon_emerald"] = module
    spec.loader.exec_module(module)

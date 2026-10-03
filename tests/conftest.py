"""Load the plugin package from a checkout without installing host dependencies."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
if os.environ.get('HERMES_AGENT_PATH'):
    sys.path.insert(0,os.environ['HERMES_AGENT_PATH'])
if 'hermes_aeon' not in sys.modules:
    spec=importlib.util.spec_from_file_location('hermes_aeon',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
    module=importlib.util.module_from_spec(spec)
    sys.modules['hermes_aeon']=module
    spec.loader.exec_module(module)

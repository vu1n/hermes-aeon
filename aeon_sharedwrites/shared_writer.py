"""Import the canonical writer used by Hermes; no duplicate implementation."""
import importlib.util
import sys
from pathlib import Path

path = Path(__file__).resolve().parent.parent / 'store' / 'shared_writer.py'
spec = importlib.util.spec_from_file_location(__name__, path)
module = importlib.util.module_from_spec(spec)
sys.modules[__name__] = module
spec.loader.exec_module(module)

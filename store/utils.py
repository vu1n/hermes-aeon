"""JSON hydration helpers independent of the host agent's utilities."""
import json

def safe_json_loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default

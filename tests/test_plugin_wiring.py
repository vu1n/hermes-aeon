"""Plugin wiring that must hold without the Hermes host runtime.

The memory-provider discovery path loads this repo as a flat plugin package
(``_hermes_user_memory.hermes-aeon``) and does not register nested packages, so
``__init__.py`` pre-installs subpackages at import time. The provider lazily
imports ``.brain_service`` (gateway/client/policy) — those imports must resolve
as plugin subpackages, including the ``aeon_readonly`` namespace parent that
``brain_service.policy`` reaches into for the restricted-content screen.
"""
import importlib


def test_brain_service_imports_as_plugin_subpackage():
    hermes = importlib.import_module("hermes_aeon.brain_service.hermes")
    assert callable(hermes.service)


def test_gateway_and_client_import_as_plugin_subpackages():
    gateway = importlib.import_module("hermes_aeon.brain_service.gateway")
    client = importlib.import_module("hermes_aeon.brain_service.client")
    assert hasattr(gateway, "Gateway")
    assert hasattr(client, "Client")


def test_aeon_readonly_namespace_reachable_for_policy():
    """brain_service.policy pulls UNSAFE from the sibling aeon_readonly projection."""
    policy = importlib.import_module("hermes_aeon.brain_service.policy")
    projection_policy = importlib.import_module(
        "hermes_aeon.aeon_readonly.projection.policy")
    assert policy.UNSAFE is projection_policy.UNSAFE

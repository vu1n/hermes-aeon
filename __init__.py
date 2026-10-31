"""aeon-emerald — Hermes memory provider that saves ended sessions to Aeon Emerald.

This repository is the plugin: link it as ``$HERMES_HOME/plugins/aeon-emerald`` and select it with
``memory.provider: aeon-emerald``. The cron jobs in ``jobs/`` share ``client.py``.
"""


def register(ctx) -> None:
    from .provider import EmeraldSessionProvider
    ctx.register_memory_provider(EmeraldSessionProvider())

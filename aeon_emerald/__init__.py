"""aeon-emerald — Hermes memory provider that saves ended sessions to Aeon Emerald.

Installed as its own plugin directory (``$HERMES_HOME/plugins/aeon-emerald`` -> this folder) and
selected with ``memory.provider: aeon-emerald``. The cron jobs in ``jobs/`` share ``client.py``.
"""


def register(ctx) -> None:
    from .provider import EmeraldSessionProvider
    ctx.register_memory_provider(EmeraldSessionProvider())

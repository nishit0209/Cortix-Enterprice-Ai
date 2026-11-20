from django.apps import AppConfig


class Nl2sqlConfig(AppConfig):
    name = "apps.nl2sql"
    label = "nl2sql"
    verbose_name = "NL to SQL"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from apps.ai.reembed import Reembedder, register_reembedder

        from .cache import REEMBED_SCAN_SQL, reembed_missing

        # cache rows stored while the local embedding model was unavailable join the semantic cache later
        register_reembedder(Reembedder("nl2sql.cache", REEMBED_SCAN_SQL, reembed_missing))

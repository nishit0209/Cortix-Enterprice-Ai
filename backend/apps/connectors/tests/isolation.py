"""Isolation-matrix factories for /data-sources routes (see backend/testing/isolation.py)."""

from __future__ import annotations


def data_source_factory(world) -> dict[str, str]:
    from apps.connectors.models import Credential, DataSource
    from apps.core.crypto import encrypt_secret

    source = world.resources.get("data_source")
    if source is None:
        with world.tenant():
            cred = Credential(kind="db_password")
            aad = f"org:{world.org.id}:credential:{cred.id}"
            cred.blob = encrypt_secret("synthetic-isolation-pw", aad=aad)
            cred.kek_id, cred.aad = cred.blob["kek_id"], aad
            cred.save()
            source = DataSource.objects.create(
                name=f"Isolation source {world.slug}", type="postgresql", username="nexora_ro", credential=cred,
                config={"host": "db.example.com", "port": 5432, "dbname": "shop", "sslmode": "require",
                        "schema": "sales"},
                status="active", verified_readonly=True,
            )
        world.resources["data_source"] = source
    return {"pk": str(source.id)}


ISOLATION_FACTORIES = {
    name: data_source_factory
    for name in ("data-source-detail", "data-source-test", "data-source-introspect", "data-source-import",
                 "data-source-imports")
}

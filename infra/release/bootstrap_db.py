import os
from uuid import UUID

import psycopg
from azure.identity import ManagedIdentityCredential
from psycopg import sql
from sqlalchemy.engine import make_url

from edu_app.config import Settings
from edu_app.db import Database
from edu_app.migration import migrate


def ensure_role(connection, role: str, object_id: str, tenant: str) -> None:
    expected = ("service", object_id, tenant, 0)
    record = connection.execute(
        "SELECT principaltype, objectid, tenantid, isadmin "
        "FROM pg_catalog.pgaadauth_list_principals(false) WHERE rolname = %s",
        (role,),
    ).fetchone()
    if record is None:
        connection.execute(
            "SELECT * FROM pg_catalog.pgaadauth_create_principal_with_oid(%s, %s, 'service', false, false)",
            (role, object_id),
        )
    elif record != expected:
        raise RuntimeError(f"Unexpected Entra principal for PostgreSQL role {role}")


def main() -> None:
    settings = Settings()
    if settings.mode != "azure" or settings.managed_identity_client_id is None:
        raise ValueError("Database bootstrap requires the Azure migration identity")
    url = make_url(settings.dsn)
    roles = {
        os.environ["EDU_DB_API_ROLE"]: str(UUID(os.environ["EDU_DB_API_OBJECT_ID"])),
        os.environ["EDU_DB_WORKER_ROLE"]: str(UUID(os.environ["EDU_DB_WORKER_OBJECT_ID"])),
    }
    if len(roles) != 2 or len(set(roles.values())) != 2 or url.username in roles:
        raise ValueError("Expected two distinct workload principals separate from migration")
    database_name = sql.Identifier(url.database)
    grantees = sql.SQL(", ").join(map(sql.Identifier, roles))
    with ManagedIdentityCredential(client_id=str(settings.managed_identity_client_id)) as credential:
        token = credential.get_token("https://ossrdbms-aad.database.windows.net/.default").token
        with psycopg.connect(
            host=url.host,
            dbname="postgres",
            user=url.username,
            password=token,
            sslmode="verify-full",
            sslrootcert="/etc/ssl/certs/ca-certificates.crt",
            connect_timeout=10,
            autocommit=True,
        ) as admin:
            admin.execute("SELECT pg_catalog.pg_advisory_lock(742010528)")
            for role, object_id in roles.items():
                ensure_role(admin, role, object_id, str(settings.entra_tenant_id))
            result = admin.execute(
                "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname = %s",
                (url.database,),
            ).fetchone()
            if result is None:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(database_name))
            elif result != (url.username,):
                raise RuntimeError("The application database is not owned by the migration principal")
            admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database_name))
            admin.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database_name, grantees))

            database = Database(settings, credential=credential)
            try:
                migrate(database)
                with database.engine.begin() as conn:
                    raw = conn.connection.driver_connection
                    raw.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                    raw.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(grantees))
                    raw.execute(
                        sql.SQL(
                            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                        ).format(grantees)
                    )
                    raw.execute(
                        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON public.alembic_version FROM {}").format(
                            grantees
                        )
                    )
            finally:
                database.engine.dispose()
    print("PostgreSQL principals and migrations are ready.")


if __name__ == "__main__":
    main()

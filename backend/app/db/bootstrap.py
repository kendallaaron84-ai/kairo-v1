"""Create the runtime login without placing credentials in Alembic history."""

from sqlalchemy import create_engine, text

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.runtime_database_password:
        raise RuntimeError("KAIRO_RUNTIME_PASSWORD is required")
    if settings.runtime_database_user != "kairo_runtime":
        raise RuntimeError("frozen migration expects KAIRO_RUNTIME_USER=kairo_runtime")

    admin_engine = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    escaped_password = settings.runtime_database_password.replace("'", "''")
    with admin_engine.connect() as connection:
        connection.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kairo_runtime') THEN "
                f"CREATE ROLE kairo_runtime LOGIN PASSWORD '{escaped_password}'; "
                "ELSE "
                f"ALTER ROLE kairo_runtime LOGIN PASSWORD '{escaped_password}'; "
                "END IF; END $$;"
            )
        )


if __name__ == "__main__":
    main()

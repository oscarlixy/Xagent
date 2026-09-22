from sqlalchemy.orm import Session


def ensure_sqlite_outer_transaction(session: Session) -> None:
    """Prevent a first SAVEPOINT from becoming a standalone SQLite transaction."""

    if not hasattr(session, "get_bind") or session.get_bind().dialect.name != "sqlite":
        return
    connection = session.connection()
    driver_connection = connection.connection.dbapi_connection
    if driver_connection is not None and not bool(
        getattr(driver_connection, "in_transaction", False)
    ):
        connection.exec_driver_sql("BEGIN")

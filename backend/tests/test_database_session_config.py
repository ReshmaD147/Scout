from scout.db import session as db_session


def test_database_url_defaults_to_local_sqlite():
    url = db_session.normalize_database_url(None)

    assert url.startswith("sqlite:///")
    assert url.endswith("retail.db")
    assert db_session.engine_connect_args(url) == {"check_same_thread": False}


def test_database_url_uses_psycopg_for_railway_postgres():
    assert (
        db_session.normalize_database_url("postgres://user:pass@host:5432/railway")
        == "postgresql+psycopg://user:pass@host:5432/railway"
    )
    assert (
        db_session.normalize_database_url("postgresql://user:pass@host:5432/railway")
        == "postgresql+psycopg://user:pass@host:5432/railway"
    )
    assert db_session.engine_connect_args("postgresql+psycopg://user:pass@host:5432/railway") == {}

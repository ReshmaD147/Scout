import contextlib
import io

from scout.db.seed import seed


def pytest_sessionstart(session):
    with contextlib.redirect_stdout(io.StringIO()):
        seed()

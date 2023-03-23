import pytest
import logging


@pytest.fixture(autouse=True)
def set_caplog_level(caplog: pytest.LogCaptureFixture):
    """Otherwise the INFO logs would not be visible."""
    caplog.set_level(logging.INFO)

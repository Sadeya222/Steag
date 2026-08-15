"""Shared fixtures for the test suite."""

import pytest

from helpers import get_relay_url, start_blossom_server


@pytest.fixture(scope="module")
def relay_url() -> str:
    return get_relay_url()


@pytest.fixture(scope="module")
def blossom_server() -> str:
    return start_blossom_server()

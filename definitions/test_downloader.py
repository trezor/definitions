from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from . import downloader as dl

# Mixed case on purpose. Real cursors are case-sensitive base64url, and the
# test must fail if something lower-cases it on the way out (as `_download_json`
# used to do with its params): an all-lowercase cursor would survive that unchanged.
CURSOR = "eyJ2IjoxLCJzIjoiTWl4ZWRDYXNl"


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self.payload


class _FakeSession:
    """Serves canned pages per endpoint, and records the requests made."""

    def __init__(self, pages: dict[str, list[Any]]) -> None:
        self.pages = pages  # {endpoint: [page, page, ...]}
        self.requests: list[str] = []

    def get(
        self, url: str, params: str | None = None, timeout: float | None = None
    ) -> _FakeResponse:
        if params:  # `requests` appends them to the URL
            url = f"{url}?{params}"
        self.requests.append(url)
        parsed = urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        query = parse_qs(parsed.query)
        # a request carrying a cursor asks for the next page
        index = 1 if "cursor" in query else 0
        return _FakeResponse(self.pages[endpoint][index])


def _serve(downloader: dl.Downloader, pages: dict[str, list[Any]]) -> _FakeSession:
    """Make the downloader talk to canned `pages` instead of the network."""
    session = _FakeSession(pages)
    downloader.session = cast(requests.Session, session)
    return session


@pytest.fixture
def downloader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dl.Downloader:
    monkeypatch.setattr(dl, "CACHE_PATH", tmp_path / "cache.json")
    return dl.Downloader(refresh=True)


def _vault(address: str, version: str) -> dict[str, Any]:
    return {"chain_id": 1, "vault_address": address, "version": version}


def test_morpho_vaults_follow_the_cursor_without_altering_it(
    downloader: dl.Downloader,
) -> None:
    first, second = _vault("0x01", "1.0"), _vault("0x02", "1.0")
    session = _serve(
        downloader,
        {
            "vaults-v1": [
                {"data": [first], "cursor": CURSOR},
                {"data": [second]},
            ],
            "vaults-v2": [{"data": []}],
        },
    )

    assert downloader.get_morpho_vaults() == [first, second]

    v1_requests = [u for u in session.requests if "vaults-v1" in u]
    assert len(v1_requests) == 2
    assert parse_qs(urlparse(v1_requests[0]).query) == {"limit": ["1000"]}
    # lower-casing the cursor makes the real API answer "400 Invalid cursor"
    assert parse_qs(urlparse(v1_requests[1]).query)["cursor"] == [CURSOR]


def test_morpho_vaults_combine_v1_and_v2(downloader: dl.Downloader) -> None:
    v1, v2 = _vault("0x01", "1.0"), _vault("0x02", "2.0")
    _serve(downloader, {"vaults-v1": [{"data": [v1]}], "vaults-v2": [{"data": [v2]}]})

    assert downloader.get_morpho_vaults() == [v1, v2]


def test_morpho_vaults_stop_on_an_empty_page_even_if_a_cursor_comes_back(
    downloader: dl.Downloader,
) -> None:
    session = _serve(
        downloader,
        {
            "vaults-v1": [
                {"data": [_vault("0x01", "1.0")], "cursor": CURSOR},
                {"data": [], "cursor": CURSOR},  # would loop forever otherwise
            ],
            "vaults-v2": [{"data": []}],
        },
    )

    assert len(downloader.get_morpho_vaults()) == 1
    assert len(session.requests) == 3


def test_coingecko_cache_keys_are_stable(downloader: dl.Downloader) -> None:
    # These keys are what `definitions-cache.json` already holds, so they must not
    # change: `--no-refresh` replays that file and fails on any key it lacks.
    _serve(downloader, {"list": [[]], "markets": [[]]})

    downloader.get_coingecko_coins_list()
    downloader.get_coingecko_top100()

    assert set(downloader.cache) == {
        "https://api.coingecko.com/api/v3/coins/list?include_platform=true",
        "https://api.coingecko.com/api/v3/coins/markets"
        "?order=market_cap_desc&page=1&per_page=100&sparkline=false&vs_currency=usd",
    }


def test_morpho_token_is_looked_up_by_chain_and_address(
    downloader: dl.Downloader,
) -> None:
    token = {"chain_id": 1, "address": "0xAbC", "decimals": 18}
    session = _serve(downloader, {"1:0xAbC": [{"data": token}]})

    assert downloader.get_morpho_token(1, "0xAbC") == token
    assert session.requests == ["https://api.morpho.org/v0/tokens/1:0xAbC"]

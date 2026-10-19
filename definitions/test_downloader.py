from urllib.parse import parse_qs, urlparse

import pytest

from . import downloader as dl

# Mixed case on purpose. Real cursors are case-sensitive base64url, and the
# test must fail if something lower-cases it on the way out (as `_download_json`
# does with `url_params`): an all-lowercase cursor would survive that unchanged.
CURSOR = "eyJ2IjoxLCJzIjoiTWl4ZWRDYXNl"


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _FakeSession:
    """Serves canned pages per endpoint, and records the requests made."""

    def __init__(self, pages):
        self.pages = pages  # {endpoint: [page, page, ...]}
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append(url)
        parsed = urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        query = parse_qs(parsed.query)
        # a request carrying a cursor asks for the next page
        index = 1 if "cursor" in query else 0
        return _FakeResponse(self.pages[endpoint][index])


@pytest.fixture
def downloader(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "CACHE_PATH", tmp_path / "cache.json")
    return dl.Downloader(refresh=True)


def _vault(address, version):
    return {"chain_id": 1, "vault_address": address, "version": version}


def test_morpho_vaults_follow_the_cursor_without_altering_it(downloader):
    first, second = _vault("0x01", "1.0"), _vault("0x02", "1.0")
    downloader.session = _FakeSession(
        {
            "vaults-v1": [
                {"data": [first], "cursor": CURSOR},
                {"data": [second]},
            ],
            "vaults-v2": [{"data": []}],
        }
    )

    assert downloader.get_morpho_vaults() == [first, second]

    v1_requests = [u for u in downloader.session.requests if "vaults-v1" in u]
    assert len(v1_requests) == 2
    assert parse_qs(urlparse(v1_requests[0]).query) == {"limit": ["1000"]}
    # lower-casing the cursor makes the real API answer "400 Invalid cursor"
    assert parse_qs(urlparse(v1_requests[1]).query)["cursor"] == [CURSOR]


def test_morpho_vaults_combine_v1_and_v2(downloader):
    v1, v2 = _vault("0x01", "1.0"), _vault("0x02", "2.0")
    downloader.session = _FakeSession(
        {"vaults-v1": [{"data": [v1]}], "vaults-v2": [{"data": [v2]}]}
    )

    assert downloader.get_morpho_vaults() == [v1, v2]


def test_morpho_vaults_stop_on_an_empty_page_even_if_a_cursor_comes_back(downloader):
    downloader.session = _FakeSession(
        {
            "vaults-v1": [
                {"data": [_vault("0x01", "1.0")], "cursor": CURSOR},
                {"data": [], "cursor": CURSOR},  # would loop forever otherwise
            ],
            "vaults-v2": [{"data": []}],
        }
    )

    assert len(downloader.get_morpho_vaults()) == 1
    assert len(downloader.session.requests) == 3

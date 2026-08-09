"""Coin-agnostic downloading/caching of definition source data (CoinGecko etc.)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HERE = Path(__file__).parent

CACHE_PATH = HERE / "definitions-cache.json"


class CacheableError(Exception):
    def __init__(self, error_code: int):
        self.error_code = error_code


class CachedDict(dict[str, Any]):
    """Generic cache object that caches to json."""

    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self.dirty = False
        if not self.cache_file.exists():
            self.cache_file.write_text("{}\n")
        self.load()

    def is_valid(self) -> bool:
        return not self._is_empty() and not self._is_expired()

    def _is_empty(self) -> bool:
        return len(self) == 0

    def _is_expired(self) -> bool:
        mtime = self.cache_file.stat().st_mtime if self.cache_file.exists() else 0
        time_diff = time.time() - mtime
        return time_diff > 3600

    def load(self) -> None:
        self.clear()
        self.update(json.loads(self.cache_file.read_text()))

    def save(self, force: bool = False) -> None:
        if not self.dirty and not force:
            return
        jsontext = json.dumps(self, sort_keys=True, indent=1)
        self.cache_file.write_text(jsontext + "\n")
        self.dirty = False

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.dirty = True


class Downloader:
    """Class that handles all the downloading and caching of definitions data."""

    def __init__(
        self, refresh: bool | None = None, sleep_duration: float = 0.0
    ) -> None:
        """
        Args:
            refresh: If True, force refresh of data. If False, use cached data. If None,
            use cached data if available, otherwise force refresh.
        """
        self.cache = CachedDict(CACHE_PATH)
        self.sleep_duration = sleep_duration
        self.refresh = refresh
        if refresh is None and not self.cache.is_valid():
            self.refresh = True
        self._init_requests_session()

    def save_cache(self):
        self.cache.save()

    def _download_json(self, url: str, **url_params: Any) -> Any:
        params = None
        encoded_params = None
        key = url

        # convert params to lower-case strings (especially for boolean values
        # because for CoinGecko API "True" != "true")
        if url_params:
            params = {key: str(value).lower() for key, value in url_params.items()}
            encoded_params = urlencode(sorted(params.items()))
            key += "?" + encoded_params

        if self.refresh is False and key not in self.cache:
            # refresh was explicitly disabled and key not found in cache
            raise ValueError(f"Key {key} not found in cache")

        if self.refresh is not True:
            # refresh was not explicitly enabled, so use cached data if available
            cached_result = self.cache.get(key)
            if cached_result is not None:
                if isinstance(cached_result, dict) and "error" in cached_result:
                    raise CacheableError(cached_result["error"])
                return cached_result

        logging.info(f"Fetching data from {url}")

        r = self.session.get(url, params=encoded_params, timeout=60)
        if r.status_code == requests.codes.forbidden:
            self.cache[key] = {"error": r.status_code}
            raise CacheableError(r.status_code)
        r.raise_for_status()
        data = r.json()
        self.cache[key] = data
        if self.sleep_duration:
            time.sleep(self.sleep_duration)
        return data

    def _init_requests_session(self) -> None:
        self.session = requests.Session()
        # As CoinGecko API will block us after ~30 requests for the whole minute,
        # we need a way to retry the request multiple times.
        retries = Retry(total=5, status_forcelist=[502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def get_coingecko_asset_platforms(self) -> Any:
        url = "https://api.coingecko.com/api/v3/asset_platforms"
        return self._download_json(url)

    def get_defillama_chains(self) -> Any:
        url = "https://api.llama.fi/chains"
        return self._download_json(url)

    def get_coingecko_tokens_for_network(self, coingecko_network_id: str) -> list[Any]:
        url = f"https://tokens.coingecko.com/{coingecko_network_id}/all.json"
        try:
            data = self._download_json(url)
            return data.get("tokens", [])
        except CacheableError:
            # "Forbidden" is raised by Coingecko if no tokens are available under specified id
            pass
        except requests.exceptions.HTTPError as err:
            raise err

        return []

    def get_coingecko_coins_list(self) -> Any:
        url = "https://api.coingecko.com/api/v3/coins/list"
        return self._download_json(url, include_platform=True)

    def get_coingecko_top100(self) -> Any:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        return self._download_json(
            url,
            vs_currency="usd",
            order="market_cap_desc",
            per_page=100,
            page=1,
            sparkline=False,
        )

"""Shared RPC helpers: load the RPC URL from .env, redact it, and make JSON-RPC calls with retries.

The RPC URL contains the provider API key, so it must never appear in output. Every
error message that leaves this module goes through `redact()`.
"""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from web3 import Web3

ARBITRUM_CHAIN_ID = 42161
# Provider-side JSON-RPC errors that are retried (seen on Alchemy: "layer stale" from a lagging
# archive backend). Anything else is a real error and raises at once.
TRANSIENT_MESSAGES = ("layer stale",)


def load_rpc_url() -> str:
    load_dotenv()
    url = os.environ.get("ARBITRUM_RPC_URL", "").strip()
    if not url:
        raise RuntimeError("ARBITRUM_RPC_URL is not set (copy .env.example to .env and fill it in)")
    return url


def provider_name(url: str) -> str:
    """Host name only (e.g. 'arb-mainnet.g.alchemy.com'): safe to log and save with results."""
    return urlparse(url).hostname or "unknown"


def redact(text: object, url: str) -> str:
    """Remove the full URL and its path (where providers put the API key) from a message."""
    s = str(text)
    path = urlparse(url).path.strip("/")
    for secret in (url, path, path.rsplit("/", 1)[-1]):
        if secret:
            s = s.replace(secret, "<redacted>")
    return s


def _retrying_session(max_retries: int = 8, backoff_s: float = 0.5) -> requests.Session:
    """Session that retries HTTP 429/5xx with exponential backoff, like RpcClient. POST is
    retried too: this repo only sends read calls (eth_call, eth_getLogs, ...), which are
    idempotent. It never sends transactions."""
    retry = Retry(total=max_retries, backoff_factor=backoff_s, backoff_max=30,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=None,
                  respect_retry_after_header=True, raise_on_status=False)
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def get_web3(url: str | None = None) -> Web3:
    url = url or load_rpc_url()
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}, session=_retrying_session()))
    chain_id = w3.eth.chain_id
    if chain_id != ARBITRUM_CHAIN_ID:
        raise RuntimeError(f"RPC is on chain {chain_id}, expected Arbitrum One ({ARBITRUM_CHAIN_ID})")
    return w3


class RpcError(Exception):
    def __init__(self, code: int | None, message: str):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class RpcClient:
    """Minimal JSON-RPC client with exponential-backoff retries on rate limits and transient
    network/server errors. Non-transient JSON-RPC errors are raised immediately (redacted)."""

    def __init__(self, url: str, max_retries: int = 8, backoff_s: float = 0.5, timeout_s: float = 30):
        self.url = url
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.calls = 0

    def call(self, method: str, params: list):
        last = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                time.sleep(min(self.backoff_s * 2 ** (attempt - 1), 30))
            try:
                self.calls += 1
                r = self.session.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=self.timeout_s,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last = f"{type(e).__name__}: {e}"
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                continue
            try:
                body = r.json()
            except ValueError:
                last = f"HTTP {r.status_code}, non-JSON body"
                continue
            err = body.get("error")
            if err is None:
                return body["result"]
            code, msg = err.get("code"), str(err.get("message", ""))
            if code == 429 or "rate limit" in msg.lower() or "exceeded its compute units" in msg.lower():
                last = f"rate limited ({code})"
                continue
            if any(t in msg.lower() for t in TRANSIENT_MESSAGES):
                last = f"transient provider error ({code}): {redact(msg, self.url)}"
                continue
            raise RpcError(code, redact(msg, self.url))
        raise RpcError(None, redact(f"gave up after {self.max_retries} retries; last: {last}", self.url))

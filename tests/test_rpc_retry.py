"""The web3 provider session retries rate limits like RpcClient (no network)."""

from data.rpc import _retrying_session


def test_retrying_session_retries_429_on_post():
    s = _retrying_session(max_retries=5)
    retry = s.get_adapter("https://example.invalid").max_retries
    assert retry.total == 5 and 429 in retry.status_forcelist
    assert retry.is_retry("POST", 429) and retry.is_retry("POST", 503)
    assert not retry.is_retry("POST", 400)


def test_rpc_client_retries_layer_stale(monkeypatch):
    from data.rpc import RpcClient

    class R:
        status_code = 200

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    replies = iter([R({"error": {"code": -32000, "message": "layer stale"}}), R({"result": "0x1"})])
    c = RpcClient("https://example.invalid/key", backoff_s=0)
    monkeypatch.setattr(c.session, "post", lambda *a, **k: next(replies))
    assert c.call("eth_blockNumber", []) == "0x1" and c.calls == 2

from comani import HTTPTransport


async def test_http_waits_and_transfers_do_not_share_control_connections(monkeypatch):
    sessions = []

    class Connector:
        def __init__(self, *, limit):
            self.limit = limit

    class Response:
        def __init__(self, method):
            self.method = method

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"result": {"method": self.method}}

        def raise_for_status(self):
            pass

    class Session:
        def __init__(self, *, connector, **kwargs):
            self.connector = connector
            self.methods = []
            self.closed = False
            sessions.append(self)

        def post(self, url, *, json):
            self.methods.append(json["method"])
            return Response(json["method"])

        async def close(self):
            self.closed = True

    monkeypatch.setattr("comani.transport.aiohttp.TCPConnector", Connector)
    monkeypatch.setattr("comani.transport.aiohttp.ClientSession", Session)
    transport = HTTPTransport("http://example.invalid")
    await transport.open()
    for method in ("wait_result", "heartbeat", "submit", "fetch", "artifact_put", "close_session"):
        assert await transport.request(method) == {"method": method}
    control, waiting, data = sessions
    assert control.methods == ["heartbeat", "submit", "close_session"]
    assert waiting.methods == ["wait_result"]
    assert data.methods == ["fetch", "artifact_put"]
    assert [session.connector.limit for session in sessions] == [32, 100, 4]
    await transport.close()
    assert all(session.closed for session in sessions)

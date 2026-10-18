"""Real HubClient websocket transport; isolated GA with no model/API calls."""
import asyncio
import json
import queue
import sys
from types import SimpleNamespace
from pathlib import Path

from test_bridge_sessions import manager, tmp_ga_root, _mod, _make_stub
from frontends import hub


class FakeAgent:
    def __init__(self):
        self.llm_no = 0
        self.is_running = False
        self.intervene = ""
        self.all_outputs = []
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(name="zero", history=[]))

    def run(self):
        pass

    def list_llms(self):
        return [(0, "zero", None), (1, "one", None)]

    def next_llm(self, no):
        self.llm_no = no
        self.llmclient.backend.name = ["zero", "one"][no]

    def put_task(self, text, **kwargs):
        self.q = queue.Queue()
        self.is_running = text == "hold"
        self.all_outputs.append({"input": text, "outputs": ["answer:" + text]})
        if not self.is_running:
            self.q.put({"done": "answer:" + text, "outputs": ["answer:" + text]})
        return self.q

    def abort(self):
        self.is_running = False
        if hasattr(self, "q"):
            self.q.put({"done": "stopped"})


def test_desktop_hub_roundtrip(manager, monkeypatch):
    from websockets.asyncio.server import serve
    monkeypatch.setattr(hub, "serve", lambda: None)
    monkeypatch.setattr(manager, "ensure_ga_import_path", lambda: Path(manager.ga_root))
    monkeypatch.setitem(sys.modules, "agentmain", _make_stub("agentmain", {"GenericAgent": FakeAgent}))
    monkeypatch.setattr(sys.modules["plan_state"], "sync_plan_path_from_text", lambda *a: None, raising=False)

    async def exercise():
        arrivals = asyncio.Queue()
        peers = []

        async def accepted(ws):
            hello = json.loads(await ws.recv())
            replies = asyncio.Queue()
            await arrivals.put((ws, hello, replies))
            async for message in ws:
                await replies.put(json.loads(message))

        async def rpc(peer, op, **kwargs):
            ws, _, replies = peer
            await ws.send(json.dumps({"op": op, "id": 1, **kwargs}))
            return (await asyncio.wait_for(replies.get(), 3))["data"]

        async def wait_for(predicate):
            for _ in range(150):
                if predicate():
                    return
                await asyncio.sleep(.02)
            raise AssertionError("timed out waiting for bridge/GA state")

        async with serve(accepted, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(hub, "URL", f"ws://127.0.0.1:{port}/ws")
            sessions = [manager.create_session(), manager.create_session()]
            try:
                for sess in sessions:
                    manager.make_agent(sess)
                    peers.append(await asyncio.wait_for(arrivals.get(), 3))
                assert len({p[1]["name"] for p in peers}) == 2
                assert peers[0][1]["name"] == f"desktop-{sessions[0].id}"
                assert set(peers[0][1]["caps"]) == {"get", "put", "abort", "llm", "inject"}
                assert (await rpc(peers[0], "put_task", text="hello"))["accepted"]
                await wait_for(lambda: not manager._session_has_unfinished_work(sessions[0]))
                assert [m["content"] for m in sessions[0].messages] == ["hello", "answer:hello"]
                assert not sessions[1].messages
                assert (await rpc(peers[0], "get"))["tasks"][0]["input"] == "hello"
                assert (await rpc(peers[0], "seg", i=0, j=0))["content"] == "answer:hello"
                assert (await rpc(peers[0], "llm", no=1))["cur"] == 1
                assert manager._live_model(sessions[0])["llmNo"] == 1
                assert sessions[1].agent.llm_no == 0
                assert (await rpc(peers[0], "put_task", text="hold"))["accepted"]
                await wait_for(lambda: sessions[0].agent.is_running)
                assert (await rpc(peers[0], "put_task", text="extra"))["code"] == "busy"
                assert (await rpc(peers[0], "inject", kind="intervene", text="steer"))["ok"]
                assert sessions[0].agent.intervene == "steer"
                await rpc(peers[0], "abort")
                await wait_for(lambda: not manager._session_has_unfinished_work(sessions[0]))
                assert sessions[0].status == "cancelled"
                manager._maintenance_token = "test"
                assert (await rpc(peers[1], "put_task", text="blocked"))["code"] == "busy"
                manager._maintenance_token = None
                assert manager.delete_session(sessions[0].id)
                await asyncio.wait_for(peers[0][0].wait_closed(), 3)
                assert sessions[0].agent._hub._closed
                assert manager._hub_command(sessions[0], "put", "stale")["code"] == "gone"
                assert (await rpc(peers[1], "get"))["nt"] == 0
            finally:
                manager._maintenance_token = None
                for sess in sessions:
                    if sess.agent:
                        sess.agent.abort()
                        sess.agent._hub.close()
                for ws, _, _ in peers:
                    await asyncio.wait_for(ws.wait_closed(), 3)

    asyncio.run(exercise())


def test_closed_hub_never_connects(monkeypatch):
    import websockets
    def unexpected(*args, **kwargs):
        raise AssertionError("closed HubClient must not connect")
    monkeypatch.setattr(websockets, "connect", unexpected)
    client = hub.HubClient("closed")
    client.close()
    asyncio.run(client._loop())

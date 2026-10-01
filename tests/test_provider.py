"""The provider against a stub Dexio MCP endpoint. No Hermes install needed."""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dexio_plugin", ROOT / "__init__.py")
dexio = importlib.util.module_from_spec(spec)
sys.modules["dexio_plugin"] = dexio
spec.loader.exec_module(dexio)

PAGES = {
    "customers/juniper": "# Juniper Street Cafe\n\nOur largest wholesale account.\n",
}


class Stub(BaseHTTPRequestHandler):
    calls: list = []
    status = 200
    sse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.calls.append({"auth": self.headers.get("Authorization"), **body["params"]})
        if Stub.status != 200:
            self.send_response(Stub.status)
            self.end_headers()
            return
        name, args = body["params"]["name"], body["params"]["arguments"]
        content = [{"type": "text", "text": json.dumps(reply(name, args))}]
        if name == "read_page" and args["path"] in PAGES:
            content.append({"type": "text", "text": PAGES[args["path"]]})
        is_error = name == "read_page" and args["path"] not in PAGES
        if is_error:
            content = [{"type": "text", "text": f"no page at {args['path']}"}]
        msg = {"jsonrpc": "2.0", "id": body["id"], "result": {"content": content, "isError": is_error}}
        if Stub.sse:
            data, ctype = f"event: message\ndata: {json.dumps(msg)}\n\n", "text/event-stream"
        else:
            data, ctype = json.dumps(msg), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data.encode())


def reply(name, args):
    if name == "search_pages":
        return {"results": [{"path": "customers/juniper", "title": "Juniper Street Cafe",
                             "matches": [{"line": 3, "text": "Our largest wholesale account."}]}]}
    if name == "list_pages":
        return {"pages": [{"path": p} for p in PAGES], "total": len(PAGES)}
    if name == "read_page":
        return {"path": args["path"], "version": "abc"}
    return {"ok": True, "path": args.get("path")}


@pytest.fixture()
def server(monkeypatch, tmp_path):
    Stub.calls, Stub.status, Stub.sse = [], 200, False
    httpd = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("DEXIO_API_KEY", "dxk_test")
    monkeypatch.setenv("DEXIO_URL", f"http://127.0.0.1:{httpd.server_port}")
    yield tmp_path
    httpd.shutdown()


def provider(home, **kw):
    p = dexio.DexioMemoryProvider()
    p.initialize("s1", hermes_home=str(home), platform="cli", **kw)
    return p


def test_available_only_with_a_key(monkeypatch):
    monkeypatch.delenv("DEXIO_API_KEY", raising=False)
    assert not dexio.DexioMemoryProvider().is_available()
    monkeypatch.setenv("DEXIO_API_KEY", "dxk_x")
    assert dexio.DexioMemoryProvider().is_available()


def test_recall_before_a_turn(server):
    p = provider(server)
    block = p.prefetch("who is our biggest wholesale customer?")
    assert "## From your Dexio wiki" in block
    assert "Juniper Street Cafe (`customers/juniper`)" in block
    assert "Our largest wholesale account." in block
    assert Stub.calls[-1]["name"] == "search_pages" and Stub.calls[-1]["auth"] == "Bearer dxk_test"
    n = len(Stub.calls)
    assert p.prefetch("who is our biggest wholesale customer?") == block   # cached
    assert len(Stub.calls) == n
    assert p.prefetch("") == "" and p.prefetch("hi") == ""


def test_recall_can_be_turned_off(server):
    (server / "dexio.json").write_text(json.dumps({"auto_recall": False}))
    assert provider(server).prefetch("anything at all") == ""


def test_long_messages_are_searched_by_their_opening(server):
    provider(server).prefetch("word " * 500)
    assert len(Stub.calls[-1]["arguments"]["query"]) <= dexio.QUERY_CHARS


def test_tools_and_the_agent_name(server):
    p = provider(server, agent_identity="ops")
    assert [t["name"] for t in p.get_tool_schemas()] == dexio.TOOL_NAMES
    out = json.loads(p.handle_tool_call("dexio_write", {"path": "notes/a", "text": "# A\n", "note": "start"}))
    assert out["ok"]
    sent = Stub.calls[-1]
    assert sent["name"] == "write_page" and sent["arguments"]["agent"] == "ops"
    assert sent["arguments"]["note"] == "start"
    p.handle_tool_call("dexio_edit", {"path": "notes/a", "old_text": "A", "new_text": ""})
    assert Stub.calls[-1]["arguments"]["new_text"] == ""          # empty deletes, still sent
    p.handle_tool_call("dexio_append", {"path": "log", "text": "- one"})
    assert Stub.calls[-1]["name"] == "append_page"
    read = json.loads(p.handle_tool_call("dexio_read", {"path": "customers/juniper"}))
    assert read["version"] == "abc" and "largest wholesale" in read["text"]


def test_tools_are_offered_before_initialize(server):
    # Hermes's MemoryManager reads tool schemas in add_provider(), before initialize_all().
    assert [t["name"] for t in dexio.DexioMemoryProvider().get_tool_schemas()] == dexio.TOOL_NAMES


def test_default_profile_writes_as_hermes(server):
    p = provider(server, agent_identity="default")
    p.handle_tool_call("dexio_append", {"path": "log", "text": "x"})
    assert Stub.calls[-1]["arguments"]["agent"] == "hermes"


def test_errors_come_back_as_tool_errors(server):
    p = provider(server)
    assert "no page at" in json.loads(p.handle_tool_call("dexio_read", {"path": "missing"}))["error"]
    assert "required" in json.loads(p.handle_tool_call("dexio_search", {"query": " "}))["error"]
    assert "Unknown tool" in json.loads(p.handle_tool_call("dexio_nope", {}))["error"]
    Stub.status = 401
    err = json.loads(p.handle_tool_call("dexio_list", {}))["error"]
    assert "API key" in err
    assert p.prefetch("something new to look for") == ""          # recall fails quietly


def test_event_stream_replies(server):
    Stub.sse = True
    assert "Juniper" in provider(server).prefetch("wholesale account")


def test_without_a_key_it_offers_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("DEXIO_API_KEY", raising=False)
    p = provider(tmp_path)
    assert p.get_tool_schemas() == [] and p.system_prompt_block() == "" and p.prefetch("x y z") == ""
    assert "not configured" in json.loads(p.handle_tool_call("dexio_list", {}))["error"]


def test_save_config_and_its_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("DEXIO_URL", raising=False)
    p = dexio.DexioMemoryProvider()
    p.save_config({"url": "https://wiki.example.com/", "api_key": "never-written"}, str(tmp_path))
    saved = json.loads((tmp_path / "dexio.json").read_text())
    assert saved == {"url": "https://wiki.example.com/"}
    assert dexio._load_config(str(tmp_path))["url"] == "https://wiki.example.com"
    monkeypatch.setenv("DEXIO_URL", "https://other.example.com")
    assert dexio._load_config(str(tmp_path))["url"] == "https://other.example.com"
    assert dexio._load_config(str(tmp_path / "none"))["max_results"] == 4


def test_register_hands_hermes_one_provider():
    got = []

    class Ctx:
        def register_memory_provider(self, p):
            got.append(p)

    dexio.register(Ctx())
    assert len(got) == 1 and got[0].name == "dexio"

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
        return {"pages": [{"path": p, "title": "Juniper Street Cafe"} for p in PAGES],
                "total": len(PAGES)}
    if name == "read_page":
        return {"path": args["path"], "title": "Juniper Street Cafe", "version": "abc"}
    if name == "page_history":
        return {"changes": [
            {"revision": 12, "at": "2026-11-07T18:00:00Z", "path": "customers/juniper", "op": "edit",
             "agent": "ops", "person": "Sam", "author": "key", "note": "Tuesday deliveries",
             "deleted": False},
            {"revision": 11, "at": "2026-11-07T17:00:00Z", "path": "decisions/bag-price",
             "op": "write", "agent": "support", "person": "Sam", "author": "key", "note": "",
             "deleted": False}][:args.get("limit", 15)]}
    if name == "list_workspaces":
        return {"workspace": {"handle": "demo1234", "name": "Wrenfield",
                              "url": "https://app.dexio.wiki/w/demo1234"}}
    return {"ok": True, "path": args.get("path"), "version": "v2"}


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


# -- 0.2: the activity record, live events, and Hermes's newer hooks ----------------

def activity(home):
    return dexio.read_activity(dexio.activity_path(str(home)))


@pytest.fixture()
def events(monkeypatch):
    """A stand-in for hermes_cli.plugin_events that records what the provider pushes."""
    got = []
    pkg = type(sys)("hermes_cli")
    mod = type(sys)("hermes_cli.plugin_events")
    mod.broadcast_plugin_event = lambda plugin, event, payload=None: got.append((plugin, event, payload))
    pkg.plugin_events = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", mod)
    return got


def test_a_recall_is_recorded_once_per_turn(server, events):
    p = provider(server, agent_identity="ops")
    p.on_turn_start(1, "who is our biggest wholesale customer?")
    p.prefetch("who is our biggest wholesale customer?", session_id="s1")
    p.prefetch("who is our biggest wholesale customer?", session_id="s1")   # same turn again
    rows = activity(server)
    assert len(rows) == 1
    rec = rows[0]
    assert rec["kind"] == "recall" and rec["agent"] == "ops" and rec["session_id"] == "s1"
    assert rec["turn"] == 1 and rec["primary"] is True
    assert rec["pages"] == [{"path": "customers/juniper", "title": "Juniper Street Cafe", "lines": 1}]
    assert events == [("dexio", "recall", rec)]
    p.on_turn_start(2, "who is our biggest wholesale customer?")
    p.prefetch("who is our biggest wholesale customer?", session_id="s1")   # cached search, new turn
    assert [r["turn"] for r in activity(server)] == [2, 1]


def test_tool_calls_are_recorded_with_op_and_path(server, events):
    p = provider(server, agent_identity="ops")
    p.on_turn_start(3, "file it")
    p.handle_tool_call("dexio_write", {"path": "customers/juniper", "text": "# J\n", "note": "Tuesdays"})
    p.handle_tool_call("dexio_read", {"path": "customers/juniper"})
    p.handle_tool_call("dexio_search", {"query": "wholesale"})
    p.handle_tool_call("dexio_read", {"path": "missing"})
    rows = activity(server)
    assert [(r["op"], r.get("path"), r["ok"]) for r in rows] == [
        ("read", "missing", False), ("search", None, True),
        ("read", "customers/juniper", True), ("write", "customers/juniper", True)]
    write = rows[-1]
    assert write["note"] == "Tuesdays" and write["version"] == "v2" and write["turn"] == 3
    assert rows[2]["title"] == "Juniper Street Cafe"
    assert rows[1]["pages"][0]["path"] == "customers/juniper"
    assert "no page at" in rows[0]["error"]
    assert [e[1] for e in events] == ["tool"] * 4


def test_the_record_keeps_only_the_newest_entries(tmp_path):
    log = dexio.ActivityLog(str(tmp_path), keep=40)
    for i in range(130):
        log.add({"kind": "tool", "op": "read", "path": f"p/{i}"})
    lines = dexio.activity_path(str(tmp_path)).read_text().splitlines()
    assert len(lines) <= 40 + 40 // 4
    newest = log.read(5)
    assert [e["path"] for e in newest] == ["p/129", "p/128", "p/127", "p/126", "p/125"]
    assert len({e["id"] for e in log.read()}) == len(log.read())


def test_a_torn_line_is_skipped(tmp_path):
    path = dexio.activity_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text('{"kind":"recall","pages":[]}\n{"kind":"tool","op"')
    assert dexio.read_activity(path) == [{"kind": "recall", "pages": []}]


def test_older_hermes_without_plugin_events_still_works(server, monkeypatch):
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", None)   # import fails
    p = provider(server)
    assert "Juniper" in p.prefetch("wholesale account")
    assert activity(server)[0]["kind"] == "recall"


def test_subagent_recalls_are_marked(server):
    p = provider(server, agent_context="subagent")
    p.prefetch("wholesale account")
    assert activity(server)[0]["primary"] is False


def test_recall_status_reports_the_last_recall(server, monkeypatch):
    class Status:
        def __init__(self, provider_label, count):
            self.provider_label, self.count = provider_label, count

    monkeypatch.setattr(dexio, "RecallStatus", Status)
    p = provider(server)
    assert p.recall_status() is None
    p.prefetch("wholesale account")
    st = p.recall_status()
    assert (st.provider_label, st.count) == ("Dexio", 1)
    p.prefetch("hi")
    assert p.recall_status() is None


def test_the_key_comes_from_hermes_secret_scope(monkeypatch, tmp_path):
    monkeypatch.delenv("DEXIO_API_KEY", raising=False)
    pkg = type(sys)("agent")
    mod = type(sys)("agent.secret_scope")
    mod.get_secret = lambda name, default=None: "dxk_scoped" if name == "DEXIO_API_KEY" else default
    pkg.secret_scope = mod
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    p = dexio.DexioMemoryProvider()
    assert p.is_available()
    p.initialize("s1", hermes_home=str(tmp_path))
    assert p._client.api_key == "dxk_scoped"


# -- the Desktop pane's backend (dashboard/plugin_api.py) -----------------------------

def backend():
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("dexio_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    app = fastapi.FastAPI()
    app.include_router(api.router, prefix="/api/plugins/dexio")
    return TestClient(app), api


def test_backend_routes(server, monkeypatch):
    client, api = backend()
    monkeypatch.setenv("HERMES_HOME", str(server))
    p = provider(server, agent_identity="ops")
    p.prefetch("wholesale account")
    p.handle_tool_call("dexio_append", {"path": "log", "text": "- one"})

    got = client.get("/api/plugins/dexio/activity").json()
    assert got["configured"] is True
    assert [e["kind"] for e in got["entries"]] == ["tool", "recall"]

    changes = client.get("/api/plugins/dexio/changes?limit=1").json()
    assert changes["changes"] == [{"at": "2026-11-07T18:00:00Z", "agent": "ops", "person": "Sam",
                                   "path": "customers/juniper", "op": "edit",
                                   "note": "Tuesday deliveries", "revision": 12, "deleted": False,
                                   "title": "Juniper Street Cafe"}]
    assert changes["titles"] == {"customers/juniper": "Juniper Street Cafe"}
    assert changes["wiki"]["url"] == "https://app.dexio.wiki/w/demo1234"
    n = len(Stub.calls)
    client.get("/api/plugins/dexio/changes?limit=1")                 # cached
    assert len(Stub.calls) == n
    client.get("/api/plugins/dexio/changes?limit=1&fresh=true")      # after a write
    assert len(Stub.calls) == n + 2                                   # history and titles

    st = client.get("/api/plugins/dexio/status").json()
    assert st["connected"] is True and st["pages"] == 1 and st["wiki"]["name"] == "Wrenfield"
    assert st["version"] == dexio.VERSION
    assert all(c["auth"] == "Bearer dxk_test" for c in Stub.calls)


def test_backend_without_a_key_or_server(server, monkeypatch):
    client, api = backend()
    monkeypatch.setenv("HERMES_HOME", str(server))
    monkeypatch.setenv("DEXIO_URL", "http://127.0.0.1:9")                # nothing listens
    api._CACHE.clear()
    st = client.get("/api/plugins/dexio/status").json()
    assert st["connected"] is False and "could not reach" in st["error"]
    monkeypatch.delenv("DEXIO_API_KEY")
    assert client.get("/api/plugins/dexio/changes").json()["error"] == "DEXIO_API_KEY is not set"
    assert client.get("/api/plugins/dexio/activity").json()["configured"] is False


def test_manifests_agree():
    import re
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "dexio" and manifest["api"] == "plugin_api.py"
    yaml_text = (ROOT / "plugin.yaml").read_text()
    assert re.search(r"^version: (\S+)$", yaml_text, re.M).group(1) == dexio.VERSION == manifest["version"]
    declared = re.findall(r"^  - (dexio_\w+)$", yaml_text, re.M)
    assert declared == dexio.TOOL_NAMES


def test_an_edit_without_new_text_deletes_nothing(server):
    # Found in the Desktop run: a model sent str_replace's `new_string`, and 0.1 read the missing
    # new_text as "" and deleted old_text.
    p = provider(server, agent_identity="ops")
    out = json.loads(p.handle_tool_call("dexio_edit", {"path": "customers/juniper",
                                                       "old_text": "Thursdays", "note": "x"}))
    assert "new_text is required" in out["error"]
    assert not any(c["name"] == "edit_page" for c in Stub.calls)
    out = json.loads(p.handle_tool_call("dexio_edit", {"path": "customers/juniper",
                                                       "old_text": "Thursdays", "new_text": None}))
    assert "new_text is required" in out["error"]


def test_str_replace_style_names_are_understood(server):
    p = provider(server, agent_identity="ops")
    p.handle_tool_call("dexio_edit", {"path": "customers/juniper", "old_string": "Thursdays",
                                      "new_string": "Tuesdays"})
    sent = Stub.calls[-1]["arguments"]
    assert (sent["old_text"], sent["new_text"]) == ("Thursdays", "Tuesdays")
    assert "old_string" not in sent and "new_string" not in sent
    p.handle_tool_call("dexio_edit", {"path": "a", "old_text": "x", "new_text": "y", "new_string": "z"})
    assert Stub.calls[-1]["arguments"]["new_text"] == "y"           # the documented name wins
    p.handle_tool_call("dexio_write", {"path": "notes/b", "content": "# B\n"})
    assert Stub.calls[-1]["arguments"]["text"] == "# B\n"
    assert "text is required" in json.loads(p.handle_tool_call("dexio_append", {"path": "log"}))["error"]

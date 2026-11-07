"""Dexio memory provider for Hermes Agent.

Dexio (https://dexio.wiki) is one wiki that all your agents read and write over MCP.
With this provider active, Hermes searches that wiki for the user's message before every
turn and puts the best-matching pages in front of the model, and the model gets tools to
search, read and write pages. Nothing is written automatically: the agent files what is
worth keeping, the way a person adds to a team wiki, so the wiki stays readable.

Since 0.2 the provider also keeps a short local record of what it did (the pages each
turn recalled, and every page the agent read or wrote) so Hermes Desktop's Dexio pane can
show it. The record lives in ``$HERMES_HOME/dexio/activity.jsonl`` (the last 200 entries)
and is pushed live to connected Desktop windows. It never leaves the machine.

Talks to Dexio's MCP endpoint (``<url>/mcp``) with an API key, using only the standard
library. Works with the hosted service and with a self-hosted server
(https://github.com/dexio-wiki/dexio).

Configuration
  DEXIO_API_KEY   required; an API key (``dxk_...``) from Settings > Agents in Dexio
  DEXIO_URL       optional; the server, default https://app.dexio.wiki
  $HERMES_HOME/dexio.json, optional: {"url": ..., "auto_recall": true,
      "max_results": 4, "timeout": 6}
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # inside Hermes
    from agent.memory_provider import MemoryProvider
except ImportError:  # outside Hermes (this repo's tests): a stand-in base class
    class MemoryProvider:  # type: ignore[no-redef]
        pass

try:  # Hermes with the recall indicator
    from agent.memory_provider import RecallStatus
except ImportError:
    RecallStatus = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

VERSION = "0.2.0"
PLUGIN_ID = "dexio"
DEFAULT_URL = "https://app.dexio.wiki"
KEYS_URL = "https://app.dexio.wiki/settings/agents"
CONFIG_FILE = "dexio.json"
DEFAULTS: Dict[str, Any] = {"url": DEFAULT_URL, "auto_recall": True, "max_results": 4,
                            "timeout": 6.0}
RECALL_CHARS = 3000          # the most a turn's recall block adds to the prompt
QUERY_CHARS = 300            # long messages are searched by their opening
ACTIVITY_DIR = "dexio"
ACTIVITY_FILE = "activity.jsonl"
ACTIVITY_KEEP = 200          # entries kept in the local activity record
WRITE_OPS = ("write", "edit", "append")


def _tool_error(message: str) -> str:
    try:
        from tools.registry import tool_error
        return tool_error(message)
    except Exception:
        return json.dumps({"error": message})


def _secret(name: str) -> str:
    """A credential by env-var name. Inside Hermes this honors the active profile's scope,
    so two profiles served by one process each get their own key."""
    value: Optional[str] = None
    try:
        from agent.secret_scope import get_secret
        value = get_secret(name)
    except Exception:            # older Hermes, outside Hermes, or no scope installed
        value = None
    if value is None:
        value = os.environ.get(name, "")
    return str(value or "").strip()


def _hermes_home(explicit: str = "") -> str:
    if explicit:
        return str(explicit)
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", "")


def _load_config(hermes_home: str) -> Dict[str, Any]:
    config = dict(DEFAULTS)
    try:
        saved = json.loads((Path(hermes_home) / CONFIG_FILE).read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            config.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("dexio: could not read %s; using defaults", CONFIG_FILE, exc_info=True)
    env_url = os.environ.get("DEXIO_URL", "").strip()
    if env_url:
        config["url"] = env_url
    config["url"] = str(config["url"] or DEFAULT_URL).rstrip("/")
    try:
        config["max_results"] = max(1, min(10, int(config["max_results"])))
    except (TypeError, ValueError):
        config["max_results"] = DEFAULTS["max_results"]
    try:
        config["timeout"] = max(1.0, min(30.0, float(config["timeout"])))
    except (TypeError, ValueError):
        config["timeout"] = DEFAULTS["timeout"]
    config["auto_recall"] = bool(config["auto_recall"])
    return config


def _save_config_values(values: Dict[str, Any], hermes_home: str) -> None:
    """Merge the non-secret settings in ``values`` into dexio.json; ``None`` clears one."""
    path = Path(hermes_home) / CONFIG_FILE
    current: Dict[str, Any] = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except FileNotFoundError:
        current = {}
    for key, value in (values or {}).items():
        if key not in DEFAULTS:
            continue
        if value is None or value == "":
            current.pop(key, None)
        else:
            current[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DexioError(Exception):
    pass


class DexioClient:
    """One MCP tools/call per request, over Dexio's streamable HTTP endpoint."""

    def __init__(self, url: str, api_key: str, timeout: float):
        self.endpoint = url.rstrip("/") + "/mcp"
        self.api_key = api_key
        self.timeout = timeout
        self._ids = 0
        self._lock = threading.Lock()

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._ids += 1
            rid = self._ids
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"hermes-dexio/{VERSION}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise DexioError("Dexio refused the API key (401): make a new one in "
                                 "Settings > Agents and set DEXIO_API_KEY") from None
            raise DexioError(f"Dexio answered HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DexioError(f"could not reach {self.endpoint}: {exc}") from None
        message = _jsonrpc_message(raw)
        if "error" in message:
            raise DexioError(str(message["error"].get("message") or message["error"]))
        result = message.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        if result.get("isError"):
            raise DexioError(text or "Dexio returned an error")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"text": text}
        if not isinstance(data, dict):
            data = {"result": data}
        if len(content) > 1:             # read_page: a JSON header, then the page itself
            data["text"] = content[1].get("text", "")
        return data


def _jsonrpc_message(raw: str) -> Dict[str, Any]:
    """The response is plain JSON or a short server-sent-event stream."""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload.startswith("{"):
                msg = json.loads(payload)
                if "result" in msg or "error" in msg:
                    return msg
    raise DexioError("unreadable reply from Dexio")


def format_recall(results: List[Dict[str, Any]], max_results: int) -> str:
    """The recall block for one turn: titles, paths and the matching lines."""
    if not results:
        return ""
    lines = ["## From your Dexio wiki",
             "Pages that match this message. Read one with dexio_read before relying on it."]
    for hit in results[:max_results]:
        title = hit.get("title") or hit.get("path")
        lines.append(f"- {title} (`{hit.get('path')}`)")
        for m in (hit.get("matches") or [])[:3]:
            text = " ".join(str(m.get("text", "")).split())
            if text and text != f"title: {title}":
                lines.append(f"    {text[:240]}")
    block = "\n".join(lines)
    return block if len(block) <= RECALL_CHARS else block[:RECALL_CHARS].rsplit("\n", 1)[0]


def recalled_pages(results: List[Dict[str, Any]], max_results: int) -> List[Dict[str, Any]]:
    """What a recall put in front of the model, for the activity record."""
    pages = []
    for hit in results[:max_results]:
        if not hit.get("path"):
            continue
        pages.append({"path": hit["path"], "title": hit.get("title") or hit["path"],
                      "lines": len(hit.get("matches") or [])})
    return pages


# -- the local activity record ------------------------------------------------------

_LOG_LOCKS: Dict[str, threading.Lock] = {}
_LOG_LOCKS_GUARD = threading.Lock()


def activity_path(hermes_home: str) -> Path:
    return Path(hermes_home) / ACTIVITY_DIR / ACTIVITY_FILE


class ActivityLog:
    """A capped JSON-lines record of recalls and tool calls for one Hermes profile.

    Appends one line per entry; once the file holds a quarter more than ``keep`` lines it is
    rewritten to the newest ``keep``. Desktop's pane polls it through the plugin's backend
    when live events are not reaching it."""

    def __init__(self, hermes_home: str, keep: int = ACTIVITY_KEEP):
        self.path = activity_path(hermes_home) if hermes_home else None
        self.keep = keep
        key = str(self.path)
        with _LOG_LOCKS_GUARD:
            self._lock = _LOG_LOCKS.setdefault(key, threading.Lock())
        self._seq = 0

    def add(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        self._seq += 1
        entry = {"id": f"{time.time_ns() // 1_000_000}-{os.getpid()}-{self._seq}",
                 "at": _now(), **entry}
        if not self.path:
            return entry
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
                self._trim()
        except OSError:
            logger.debug("dexio: could not write %s", self.path, exc_info=True)
        return entry

    def _trim(self) -> None:
        try:
            if self.path.stat().st_size < 4096:
                return
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        if len(lines) <= self.keep + self.keep // 4:
            return
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines[-self.keep:]) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def read(self, limit: int = ACTIVITY_KEEP) -> List[Dict[str, Any]]:
        """Newest first."""
        return read_activity(self.path, limit) if self.path else []


def read_activity(path: Optional[Path], limit: int = ACTIVITY_KEEP) -> List[Dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return []
    out: List[Dict[str, Any]] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue                      # a torn last line from a crash
        if isinstance(entry, dict):
            out.append(entry)
        if len(out) >= limit:
            break
    return out


def _emit(event: str, payload: Dict[str, Any]) -> None:
    """Push to connected Hermes Desktop windows. Older Hermes has no plugin events: skip."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except ImportError:
        return
    try:
        broadcast_plugin_event(PLUGIN_ID, event, payload)
    except Exception:
        logger.debug("dexio: could not push %s", event, exc_info=True)


TOOLS: List[Dict[str, Any]] = [
    {"name": "dexio_search",
     "description": ("Search the shared Dexio wiki (paths, titles and text). Matches a "
                     "question's words, best first, with the matching lines."),
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Words to look for"},
         "folder": {"type": "string", "description": "Only search this folder (optional)"},
         "limit": {"type": "integer", "description": "Most pages to return (default 10)"}},
         "required": ["query"]}},
    {"name": "dexio_read",
     "description": ("Read one page of the Dexio wiki: its markdown, plus its version and "
                     "the pages it links to and from."),
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Page path, e.g. customers/acme"},
         "section": {"type": "string", "description": "Only this heading's section (optional)"}},
         "required": ["path"]}},
    {"name": "dexio_list",
     "description": "List the wiki's pages, or one folder's, with titles and descriptions.",
     "parameters": {"type": "object", "properties": {
         "folder": {"type": "string", "description": "Folder to list (optional)"}}}},
    {"name": "dexio_write",
     "description": ("Create a page or replace its whole text in the Dexio wiki. Search first "
                     "and update the existing page rather than creating a duplicate; use "
                     "dexio_edit for part of a page. Link related pages with [[path]]."),
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Page path, e.g. decisions/pricing"},
         "text": {"type": "string", "description": "The whole page, in markdown"},
         "note": {"type": "string", "description": "One line: what changed and why"},
         "base_version": {"type": "string",
                          "description": "Fail if the page changed since this version"}},
         "required": ["path", "text"]}},
    {"name": "dexio_edit",
     "description": ("Change part of a Dexio page: replace an exact piece of text "
                     "(old_text, unique on the page) with new_text."),
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"},
         "old_text": {"type": "string", "description": "Exact text to replace"},
         "new_text": {"type": "string",
                      "description": "The replacement text (required; an empty string deletes old_text)"},
         "note": {"type": "string", "description": "One line: what changed and why"}},
         "required": ["path", "old_text", "new_text"]}},
    {"name": "dexio_append",
     "description": "Add text to the end of a Dexio page, creating it if needed (logs, lists).",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"},
         "text": {"type": "string"},
         "note": {"type": "string", "description": "One line: what changed and why"}},
         "required": ["path", "text"]}},
]
TOOL_NAMES = [t["name"] for t in TOOLS]


class DexioMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._client: Optional[DexioClient] = None
        self._config: Dict[str, Any] = dict(DEFAULTS)
        self._agent = "hermes"
        self._recall_cache: Dict[str, Any] = {}
        self._log = ActivityLog("")
        self._session_id = ""
        self._turn = 0
        self._recorded: Optional[tuple] = None    # (session, turn, query) of the last recall
        self._last_recall_count: Optional[int] = None
        self._primary = True

    @property
    def name(self) -> str:
        return "dexio"

    # -- availability and setup -------------------------------------------
    def is_available(self) -> bool:
        return bool(_secret("DEXIO_API_KEY"))

    def unavailable_reason(self) -> str:
        return f"set DEXIO_API_KEY to an API key from {KEYS_URL}"

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "api_key", "description": "Dexio API key (starts with dxk_)",
             "secret": True, "required": True, "env_var": "DEXIO_API_KEY", "url": KEYS_URL},
            {"key": "url", "description": "Dexio server (change it only for a self-hosted one)",
             "default": DEFAULT_URL},
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        _save_config_values({k: v for k, v in (values or {}).items() if k != "api_key"},
                            hermes_home)

    def get_status_config(self, provider_config: dict) -> dict:
        config = _load_config(_hermes_home())
        key = _secret("DEXIO_API_KEY")
        if not key:
            return {"summary": "DEXIO_API_KEY is not set"}
        try:
            pages = DexioClient(config["url"], key, config["timeout"]).call(
                "list_pages", {"limit": 1})
            total = pages.get("total", pages.get("count", "?"))
            return {"summary": f"connected to {config['url']} ({total} pages)"}
        except DexioError as exc:
            return {"summary": f"not connected: {exc}"}

    # -- lifecycle ---------------------------------------------------------
    def initialize(self, session_id: str, **kwargs) -> None:
        home = _hermes_home(str(kwargs.get("hermes_home") or ""))
        self._config = _load_config(home)
        identity = str(kwargs.get("agent_identity") or "").strip()
        # The name Dexio records on every change this agent makes.
        self._agent = identity if identity and identity != "default" else "hermes"
        key = _secret("DEXIO_API_KEY")
        self._client = DexioClient(self._config["url"], key, self._config["timeout"]) if key else None
        self._recall_cache = {}
        self._session_id = str(session_id or "")
        self._turn = 0
        self._recorded = None
        self._last_recall_count = None
        # Subagents and cron runs share the profile's record but are not what the user is
        # looking at: their recalls are kept out of the pane's "this turn" view.
        self._primary = str(kwargs.get("agent_context") or "primary") == "primary"
        self._log = ActivityLog(home)

    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        return (
            "# Dexio wiki\n"
            f"You share a wiki with the user's other agents and their team ({self._config['url']}). "
            "Before each turn, pages that match the user's message appear under "
            "\"From your Dexio wiki\". Use dexio_search and dexio_read to look things up. "
            "When you learn something other agents or people should know later (a decision "
            "and its reason, how a system works, a fact about a customer or project), file it "
            "with dexio_write, dexio_edit or dexio_append: search first and update the "
            "existing page instead of making a duplicate, link related pages with [[path]], "
            "and give a one-line note. Do not file secrets or passing chatter."
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        try:
            self._turn = int(turn_number)
        except (TypeError, ValueError):
            self._turn += 1

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall_count = None
        if not self._client or not self._config["auto_recall"]:
            return ""
        q = " ".join((query or "").split())[:QUERY_CHARS]
        if len(q) < 3:
            return ""
        sid = str(session_id or self._session_id)
        cached = self._recall_cache.get(q)
        if cached is None:
            try:
                found = self._client.call("search_pages", {
                    "query": q, "limit": self._config["max_results"], "matches_per_page": 3})
            except DexioError as exc:
                logger.debug("dexio recall failed: %s", exc)
                return ""
            results = found.get("results") or []
            cached = (format_recall(results, self._config["max_results"]),
                      recalled_pages(results, self._config["max_results"]))
            if len(self._recall_cache) > 32:
                self._recall_cache.clear()
            self._recall_cache[q] = cached
        block, pages = cached
        self._last_recall_count = len(pages) if block else 0
        self._record_recall(sid, q, pages)
        return block

    def _record_recall(self, session_id: str, query: str, pages: List[Dict[str, Any]]) -> None:
        mark = (session_id, self._turn, query)
        if mark == self._recorded:        # the same turn asking again
            return
        self._recorded = mark
        entry = self._log.add({"kind": "recall", "agent": self._agent, "session_id": session_id,
                               "turn": self._turn, "primary": self._primary,
                               "query": query[:160], "pages": pages,
                               "wiki": self._config["url"]})
        _emit("recall", entry)

    def recall_status(self):
        """What the last prefetch put in the prompt, for Hermes's recall indicator."""
        if RecallStatus is None or not self._last_recall_count:
            return None
        try:
            return RecallStatus(provider_label="Dexio", count=self._last_recall_count)
        except Exception:
            return None

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None) -> None:
        # Deliberately nothing: the wiki holds what an agent chose to file, not transcripts.
        return None

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._recall_cache = {}
        self._session_id = str(new_session_id or "")
        self._recorded = None

    def shutdown(self) -> None:
        self._client = None

    # -- tools -------------------------------------------------------------
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Hermes reads the schemas when the provider is added, before initialize(), so this
        # follows the key, not the client.
        return [dict(t) for t in TOOLS] if self.is_available() else []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._client:
            return _tool_error("Dexio is not configured: set DEXIO_API_KEY")
        args = _normalize_args(tool_name, args)
        if tool_name == "dexio_edit" and args.get("new_text") is None:
            # A missing field is not an empty one: treating it as "" would delete old_text.
            return _tool_error("new_text is required (the replacement; pass \"\" to delete old_text)")
        if tool_name in ("dexio_write", "dexio_append") and not str(args.get("text") or "").strip():
            return _tool_error("text is required")
        try:
            if tool_name == "dexio_search":
                if not str(args.get("query", "")).strip():
                    return _tool_error("query is required")
                out = self._client.call("search_pages", _pick(args, "query", "folder", "limit"))
            elif tool_name == "dexio_read":
                out = self._client.call("read_page", _pick(args, "path", "section"))
            elif tool_name == "dexio_list":
                out = self._client.call("list_pages", _pick(args, "folder"))
            elif tool_name == "dexio_write":
                out = self._client.call("write_page", {
                    **_pick(args, "path", "text", "note", "base_version"), "agent": self._agent})
            elif tool_name == "dexio_edit":
                if not str(args.get("old_text", "")):
                    return _tool_error("old_text is required")
                out = self._client.call("edit_page", {
                    **_pick(args, "path", "old_text", "note"),
                    "new_text": str(args.get("new_text") or ""),   # empty deletes old_text
                    "agent": self._agent})
            elif tool_name == "dexio_append":
                out = self._client.call("append_page", {
                    **_pick(args, "path", "text", "note"), "agent": self._agent})
            else:
                return _tool_error(f"Unknown tool: {tool_name}")
        except DexioError as exc:
            self._record_tool(tool_name, args, None, str(exc), kwargs)
            return _tool_error(str(exc))
        if tool_name in ("dexio_write", "dexio_edit", "dexio_append"):
            self._recall_cache = {}       # the next turn's recall should see the change
        self._record_tool(tool_name, args, out, "", kwargs)
        return json.dumps(out, ensure_ascii=False)

    def _record_tool(self, tool_name: str, args: Dict[str, Any], out: Optional[Dict[str, Any]],
                     error: str, kwargs: Dict[str, Any]) -> None:
        op = tool_name[len("dexio_"):] if tool_name.startswith("dexio_") else tool_name
        entry: Dict[str, Any] = {"kind": "tool", "op": op, "agent": self._agent,
                                 "session_id": str(kwargs.get("session_id") or self._session_id),
                                 "turn": self._turn, "primary": self._primary,
                                 "ok": not error, "wiki": self._config["url"]}
        path = str(args.get("path") or (out or {}).get("path") or "").strip()
        if path:
            entry["path"] = path
        if op == "search":
            entry["query"] = str(args.get("query") or "")[:160]
            entry["pages"] = recalled_pages((out or {}).get("results") or [], 10)
        if op == "read" and out:
            entry["title"] = str(out.get("title") or path)
        if op in WRITE_OPS:
            if args.get("note"):
                entry["note"] = str(args["note"])[:200]
            if out and out.get("version"):
                entry["version"] = str(out["version"])
        if error:
            entry["error"] = error[:200]
        _emit("tool", self._log.add(entry))


# Names models reach for out of habit (str_replace-style editors, "content" for a body).
# Mapped only when the documented name is absent, so an explicit argument always wins.
_ALIASES: Dict[str, Dict[str, str]] = {
    "dexio_edit": {"old_string": "old_text", "new_string": "new_text"},
    "dexio_write": {"content": "text"},
    "dexio_append": {"content": "text"},
}


def _normalize_args(tool_name: str, args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(args or {})
    for alias, name in _ALIASES.get(tool_name, {}).items():
        if alias in out:
            value = out.pop(alias)
            out.setdefault(name, value)
    return out


def _pick(args: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    return {k: args[k] for k in keys if args.get(k) not in (None, "")}


def register(ctx) -> None:
    ctx.register_memory_provider(DexioMemoryProvider())

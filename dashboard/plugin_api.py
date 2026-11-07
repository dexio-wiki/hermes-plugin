"""Backend for Dexio's pane in Hermes Desktop, mounted at /api/plugins/dexio/.

Hermes imports this file only when ``dexio`` is in ``plugins.enabled`` in config.yaml.
Three read-only routes:

  GET /activity  this profile's recent recalls, reads and writes (the local record the
                 provider keeps in $HERMES_HOME/dexio/activity.jsonl)
  GET /changes   recent changes across everyone writing to the wiki (Dexio's page_history)
  GET /status    whether the key works, which wiki it reaches, and its page count

/changes and /status call the Dexio server with the profile's DEXIO_API_KEY; /activity
reads only the local file.
"""
from __future__ import annotations

import hashlib
import importlib.util
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter

router = APIRouter()

_CORE_NAME = "hermes_dexio_plugin_core"
_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
CHANGES_TTL = 8.0
STATUS_TTL = 30.0
TITLES_TTL = 60.0


_CORE_MODULE = None
_CORE_LOCK = threading.Lock()


def _core():
    """The provider module beside this folder, loaded once by path (the plugin is not a
    package Hermes imports by name from here)."""
    global _CORE_MODULE
    with _CORE_LOCK:
        if _CORE_MODULE is None:
            path = Path(__file__).resolve().parent.parent / "__init__.py"
            spec = importlib.util.spec_from_file_location(_CORE_NAME, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _CORE_MODULE = mod
        return _CORE_MODULE


def _settings():
    core = _core()
    home = core._hermes_home()
    return core, home, core._load_config(home), core._secret("DEXIO_API_KEY")


def _scope(config: Dict[str, Any], key: str) -> str:
    """Cache key for one server and key, without keeping any part of the key itself."""
    return config["url"] + "|" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _cached(kind: str, key: str, ttl: float, build, fresh: bool = False) -> Dict[str, Any]:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _CACHE.get((kind, key))
        if hit and not fresh and now - hit[0] < ttl:
            return hit[1]
    value = build()
    with _CACHE_LOCK:
        _CACHE[(kind, key)] = (now, value)
    return value


def _workspace(client) -> Dict[str, Any]:
    try:
        ws = (client.call("list_workspaces", {}) or {}).get("workspace") or {}
    except Exception:
        return {}
    return {k: ws.get(k) for k in ("handle", "name", "url") if ws.get(k)}


def _titles(client) -> Dict[str, str]:
    """Path to title for the wiki's pages (the first 200), so the feed can show titles."""
    try:
        pages = (client.call("list_pages", {"limit": 200}) or {}).get("pages") or []
    except Exception:
        return {}
    return {p["path"]: p.get("title") or p["path"] for p in pages if p.get("path")}


def _clamp(value: Optional[int], low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


@router.get("/activity")
def activity(limit: int = 100) -> Dict[str, Any]:
    core, home, config, key = _settings()
    entries = core.read_activity(core.activity_path(home), _clamp(limit, 1, core.ACTIVITY_KEEP, 100))
    return {"entries": entries, "url": config["url"], "configured": bool(key)}


@router.get("/changes")
def changes(limit: int = 15, fresh: bool = False) -> Dict[str, Any]:
    core, home, config, key = _settings()
    if not key:
        return {"changes": [], "error": "DEXIO_API_KEY is not set"}
    n = _clamp(limit, 1, 50, 15)

    def build() -> Dict[str, Any]:
        client = core.DexioClient(config["url"], key, config["timeout"])
        try:
            out = client.call("page_history", {"limit": n})
        except core.DexioError as exc:
            return {"changes": [], "error": str(exc)}
        titles = _cached("titles", _scope(config, key), TITLES_TTL,
                         lambda: {"titles": _titles(client)}, fresh)["titles"]
        rows = []
        for c in out.get("changes") or []:
            row = {k: c.get(k) for k in ("at", "agent", "person", "path", "op", "note",
                                         "revision", "deleted")}
            row["title"] = titles.get(c.get("path") or "")
            rows.append(row)
        return {"changes": rows, "titles": titles,
                "wiki": _cached("ws", _scope(config, key), STATUS_TTL, lambda: _workspace(client))}

    return _cached("changes", f"{_scope(config, key)}|{n}", CHANGES_TTL, build, fresh)


@router.get("/status")
def status(fresh: bool = False) -> Dict[str, Any]:
    core, home, config, key = _settings()
    base = {"version": core.VERSION, "url": config["url"], "auto_recall": config["auto_recall"]}
    if not key:
        return {**base, "connected": False, "error": "DEXIO_API_KEY is not set"}

    def build() -> Dict[str, Any]:
        client = core.DexioClient(config["url"], key, config["timeout"])
        try:
            pages = client.call("list_pages", {"limit": 1})
        except core.DexioError as exc:
            return {**base, "connected": False, "error": str(exc)}
        count = pages.get("count", pages.get("total"))
        return {**base, "connected": True, "pages": count, "wiki": _workspace(client)}

    return _cached("status", _scope(config, key), STATUS_TTL, build, fresh)

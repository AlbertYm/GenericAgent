"""Isolate configuration loading from real keys, network and agent startup."""
import ast
import importlib.util
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest

ROOT = Path(__file__).resolve().parents[2]


def extract(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


@pytest.fixture
def core(tmp_path, monkeypatch):
    # Never resolve or import the user's actual mykey module.
    source = tmp_path / "mykey.py"
    def find_spec(name):
        assert name == "mykey"
        return importlib.util.spec_from_file_location(name, source) if source.exists() else None
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.delitem(sys.modules, "mykey", raising=False)
    ns = dict(os=os, sys=sys, json=json, time=time, threading=threading,
              importlib=importlib, __file__=str(tmp_path / "llmcore.py"))
    extract(ROOT / "llmcore.py", {"MyKeys", "reload_mykeys", "_load_mykeys", "__getattr__", "resolve_session", "resolve_client"}, ns)
    ns["mykeys_store"] = ns["MyKeys"]()
    yield ns, source
    sys.modules.pop("mykey", None)


def write(source, value, stamp=1700000000000000100):
    source.write_text("oai_config = " + repr({"name": "test", "model": value}) + "\n", encoding="utf-8")
    os.utime(source, ns=(stamp, stamp))


def agent_type(ns):
    tree = ast.parse((ROOT / "agentmain.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GenericAgent")
    cls.body = [n for n in cls.body if getattr(n, "name", None) == "load_llm_sessions"]
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "agentmain.py", "exec"), ns)
    return ns["GenericAgent"]


def test_two_agents_update_even_if_bridge_read_first(core):
    ns, source = core
    write(source, "A")
    built = []
    def resolve(name, cfg):
        built.append(cfg["model"])
        return SimpleNamespace(backend=SimpleNamespace(name=cfg["name"], model=cfg["model"], history=[]))
    ns["resolve_client"] = resolve
    Agent = agent_type(ns)
    a, b = Agent(), Agent()
    for ga in (a, b):
        ga.llm_no = 0
        ga.load_llm_sessions()
    history = [{"role": "user", "content": "preserve"}]
    a.llmclient.backend.history = history
    old = a.llmclient
    a.load_llm_sessions()
    assert a.llmclient is old
    write(source, "B", 1700000000000000200)
    assert ns["reload_mykeys"]()[1] is True  # bridge consumes old changed signal
    assert ns["reload_mykeys"]()[1] is False
    for ga in (a, b):
        ga.load_llm_sessions()
        assert ga.llmclient.backend.model == "B"
    assert a.llmclient.backend.history is history
    assert built == ["A", "A", "B", "B"]


def test_same_second_same_size_and_backwards_mtime(core):
    ns, source = core
    write(source, "A")
    store = ns["mykeys_store"]
    first = store.snapshot()[1]
    write(source, "B", 1700000000000000200)
    data, second = store.snapshot()
    assert data["oai_config"]["model"] == "B" and second > first
    write(source, "C", 1600000000000000000)
    data, third = store.snapshot()
    assert data["oai_config"]["model"] == "C" and third > second


def test_json_fallback_and_legacy_interfaces(core):
    ns, source = core
    source.with_suffix(".json").write_text('{"oai_config": {"model": "A"}}', encoding="utf-8")
    data, changed = ns["reload_mykeys"]()
    assert changed is True
    assert ns["__getattr__"]("mykeys") is data
    assert ns["reload_mykeys"]() == (data, False)
    assert ns["_load_mykeys"]() == data
    with pytest.raises(AttributeError): ns["__getattr__"]("unknown")


def test_failure_keeps_last_good_snapshot_and_recovers(core):
    ns, source = core
    write(source, "A")
    store = ns["mykeys_store"]
    before = store.snapshot()
    source.write_text("oai_config = {", encoding="utf-8")
    assert store.snapshot() == before
    source.unlink()
    assert store.snapshot() == before
    write(source, "B", 1700000000000000300)
    assert store.snapshot()[0]["oai_config"]["model"] == "B"


def test_force_refresh_publishes_once_for_all_readers(core):
    ns, source = core
    write(source, "A")
    store = ns["mykeys_store"]
    before = store.snapshot()[1]
    store.reload(force=True)
    after = store.snapshot()[1]
    assert after > before
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.snapshot()[1], range(32)))
    assert results == [after] * 32


def test_resolvers_accept_snapshot_without_reading_global_or_mutating_it(core):
    ns, source = core
    class Session:
        def __init__(self, cfg): self.cfg = cfg
    ns.update(LLMSession=Session, ClaudeSession=Session, NativeClaudeSession=type("NC", (), {}),
              NativeOAISession=type("NO", (), {}), ToolClient=lambda s: s)
    cfg = {"model": "A"}
    ns["reload_mykeys"] = lambda: pytest.fail("unexpected global config read")
    session = ns["resolve_client"]("oai_config", cfg)
    assert session.cfg == {"model": "A", "_mykey_name": "oai_config"}
    assert cfg == {"model": "A"}
    ns["reload_mykeys"] = lambda: ({"oai_config": cfg}, False)
    assert ns["resolve_session"]("oai_config").cfg == session.cfg


def test_bridge_save_only_writes_config_and_reads_profiles(tmp_path):
    tree = ast.parse((ROOT / "frontends/desktop_bridge.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentManager")
    method = next(n for n in cls.body if getattr(n, "name", None) == "_save_mykey_text")
    ns = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "desktop_bridge.py", "exec"), ns)
    file = tmp_path / "synthetic_config.py"
    manager = SimpleNamespace(_mykey_file=lambda: file, list_model_profiles=lambda: ["profile"])
    assert ns["_save_mykey_text"](manager, "synthetic = {}") == ["profile"]
    assert file.read_text(encoding="utf-8") == "synthetic = {}"


def test_ga_preserves_selected_model_when_config_order_changes(core):
    ns, source = core
    ns["resolve_client"] = lambda name, cfg: SimpleNamespace(backend=SimpleNamespace(name=cfg["name"], history=[]))
    Agent = agent_type(ns)
    ga = Agent()
    ga.llm_no = 1
    source.write_text("oai_config_a = {'name': 'A'}\noai_config_b = {'name': 'B'}\n", encoding="utf-8")
    ga.load_llm_sessions()
    assert ga.llmclient.backend.name == "B"
    source.write_text("oai_config_b = {'name': 'B'}\noai_config_a = {'name': 'A'}\n", encoding="utf-8")
    os.utime(source, ns=(1700000000000000000, 1700000000000000000))
    ga.load_llm_sessions()
    assert ga.llmclient.backend.name == "B" and ga.llm_no == 0


def test_ga_drops_stale_client_when_models_removed(core):
    ns, source = core
    ns["resolve_client"] = lambda name, cfg: SimpleNamespace(backend=SimpleNamespace(name=cfg["name"], history=[]))
    Agent = agent_type(ns)
    ga = Agent()
    ga.llm_no = 0
    write(source, "A")
    ga.load_llm_sessions()
    source.write_text("", encoding="utf-8")
    os.utime(source, ns=(1700000000000000500, 1700000000000000500))
    ga.load_llm_sessions()
    assert ga.llmclients == [] and ga.llmclient is None


def test_loader_does_not_reuse_same_second_bytecode(core):
    import py_compile
    ns, source = core
    write(source, "A")
    py_compile.compile(str(source), doraise=True)
    write(source, "B", 1700000000000000200)
    assert ns["mykeys_store"].snapshot()[0]["oai_config"]["model"] == "B"


def test_legacy_direct_loader_raises_and_cleans_failed_module(core):
    ns, source = core
    source.write_text("partial = 1\nraise RuntimeError('synthetic failure')\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        ns["_load_mykeys"]()
    assert "mykey" not in sys.modules
    assert ns["reload_mykeys"]() == ({}, False)

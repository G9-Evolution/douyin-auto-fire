"""本地控制台的账号隔离与配置保存回归测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import web_server
from app.config import ConfigError


def test_account_payload_never_returns_cookie_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(web_server, "_read_env", lambda: {"DOUYIN_COOKIE": "private-cookie"})
    single = web_server._account_payload()
    assert single["accounts"][0]["cookie"] is True
    assert "private-cookie" not in json.dumps(single)

    config = tmp_path / "config"
    config.mkdir()
    (config / "accounts.json").write_text(json.dumps({
        "accounts": [{"id": "future_account", "enabled": True, "env_file": ".env.future", "avatar_url": "https://cdn.example/avatar.webp"}]
    }), encoding="utf-8")
    (tmp_path / ".env.future").write_text("DOUYIN_COOKIE=private-cookie\n", encoding="utf-8")
    multi = web_server._account_payload()
    assert multi["accounts"][0]["cookie"] is True
    assert multi["accounts"][0]["avatar_url"] == "https://cdn.example/avatar.webp"
    assert "private-cookie" not in json.dumps(multi)


def test_unknown_account_cannot_read_or_write_account_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "accounts.json").write_text(json.dumps({
        "accounts": [{"id": "known", "enabled": False, "env_file": ".env.known"}]
    }), encoding="utf-8")
    replies = []

    class FakeHandler:
        _route_api = web_server.Handler._route_api

        def _send_json(self, body, status=200):
            replies.append((status, body))

        def _read_body(self):
            return {"targets": []}

        def _save_config(self, *args):
            pytest.fail("未知账号不得保存配置")

    FakeHandler()._route_api("PUT", "/api/config", {"account": ["../other"]})
    assert replies[0][0] == 400
    assert not (tmp_path / "other.json").exists()

    handler = object.__new__(web_server.Handler)
    with pytest.raises(ConfigError, match="账号不存在"):
        handler._save_accounts({"accounts": [{"id": "invented", "enabled": True}]})
    with pytest.raises(ConfigError, match="请先选择账号"):
        handler._save_config({"targets": []})
    with pytest.raises(ConfigError, match="请先选择账号"):
        handler._save_schedule({"time": "17:20"})


def test_new_account_schedule_records_scheduled_source() -> None:
    command = web_server._schedule_command("future_account")
    assert "run.py --account future_account --source scheduled" in command


def test_run_api_rejects_unvalidated_config_before_spawn(monkeypatch) -> None:
    replies = []

    class FakeHandler:
        _route_api = web_server.Handler._route_api

        def _read_body(self):
            return {"account": "account1", "dry_run": False}

        def _send_json(self, body, status=200):
            replies.append((status, body))

    monkeypatch.setattr(web_server, "_validate_run_account", lambda account: account)
    monkeypatch.setattr(web_server, "_run_lock_exists", lambda account: False)
    monkeypatch.setattr(web_server, "_account_task_path", lambda account: Path("task.json"))
    monkeypatch.setattr(web_server, "_account_artifacts_dir", lambda account: Path("artifacts"))
    monkeypatch.setattr(web_server, "require_validation", lambda *args: (_ for _ in ()).throw(
        ConfigError("未通过无发送试运行")))
    monkeypatch.setattr(web_server, "_spawn_run", lambda *args: pytest.fail("不应启动发送进程"))

    FakeHandler()._route_api("POST", "/api/run", {})
    assert replies == [(400, {"error": "未通过无发送试运行", "type": "ConfigError"})]


def test_run_requires_selected_enabled_account(monkeypatch):
    monkeypatch.setattr(web_server, "_account_payload", lambda: {
        "mode": "multi",
        "accounts": [
            {"id": "account1", "enabled": True},
            {"id": "account2", "enabled": False},
        ],
    })
    with pytest.raises(ConfigError, match="请选择"):
        web_server._validate_run_account(None)
    with pytest.raises(ConfigError, match="已停用"):
        web_server._validate_run_account("account2")
    with pytest.raises(ConfigError, match="不存在"):
        web_server._validate_run_account("missing")
    assert web_server._validate_run_account("account1") == "account1"


def test_account_run_locks_are_independent(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(web_server, "PROJECT_ROOT", tmp_path)
    lock = tmp_path / "artifacts" / "account1" / "run.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="ascii")
    assert web_server._run_lock_exists("account1")
    assert not web_server._run_lock_exists("account2")


def test_invalid_config_does_not_replace_existing_file(monkeypatch, tmp_path: Path):
    path = tmp_path / "config" / "tasks" / "account1.json"
    path.parent.mkdir(parents=True)
    original = {"friends": ["小顾"], "messages": [{"type": "text", "value": "你好"}]}
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web_server, "_account_task_path", lambda _id: path)
    replies = []
    handler = object.__new__(web_server.Handler)
    handler._send_json = lambda data, status=200: replies.append(data)

    with pytest.raises(ConfigError, match="原生表情未在"):
        handler._save_config({
            "targets": [{"name": "小顾", "messages": [{"type": "douyin_sticker", "sticker": "不存在的表情"}]}],
            "stickers": {"其他": {"fallback_index": 0}},
        }, "account1")

    assert json.loads(path.read_text(encoding="utf-8")) == original
    assert list(path.parent.glob(".account1-*.json")) == []
    assert not replies


def test_save_custom_content_returns_targets_payload(monkeypatch, tmp_path: Path):
    path = tmp_path / "config" / "tasks" / "account1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"friends": ["小顾"], "messages": [{"type": "text", "value": "你好"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web_server, "_account_task_path", lambda _id: path)
    monkeypatch.setattr(web_server, "_config_payload", lambda _id: {"targets": []})
    monkeypatch.setattr(web_server, "_account_enabled", lambda _id: False)
    replies = []
    handler = object.__new__(web_server.Handler)
    handler._send_json = lambda data, status=200: replies.append(data)

    handler._save_config({
        "global_messages": [{"type": "text", "content": "默认内容"}],
        "targets": [{"name": "小顾", "content_mode": "custom", "messages": [{"type": "text", "content": "单独内容"}]}],
    }, "account1")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["targets"][0]["content_mode"] == "custom"
    assert replies == [{"ok": True, "format": "targets", "config": {"targets": []}}]


def test_friend_run_passes_temporary_duplicate_switch(monkeypatch) -> None:
    replies = []
    spawned = []

    class FakeHandler:
        _route_api = web_server.Handler._route_api

        def _read_body(self):
            return {
                "account": "account1",
                "target": "好友A",
                "task_revision": "revision-1",
                "prevent_duplicates": False,
            }

        def _send_json(self, body, status=200):
            replies.append((status, body))

    monkeypatch.setattr(web_server, "_validate_run_account", lambda account: account)
    monkeypatch.setattr(web_server, "_account_task_path", lambda account: Path("task.json"))
    monkeypatch.setattr(web_server, "_parse_task_raw", lambda raw: {"targets": [{"name": "好友A"}]})
    monkeypatch.setattr(web_server, "_read_json", lambda path: {})
    monkeypatch.setattr(web_server, "require_revision", lambda *args: None)
    monkeypatch.setattr(web_server, "_run_lock_exists", lambda account: False)
    monkeypatch.setattr(web_server, "_account_artifacts_dir", lambda account: Path("artifacts"))
    monkeypatch.setattr(web_server, "require_validation", lambda *args: None)
    monkeypatch.setattr(
        web_server,
        "_spawn_run",
        lambda dry, account, target, revision, prevent_duplicates: spawned.append(
            (dry, account, target, revision, prevent_duplicates)
        ) or {"id": 1, "status": "running"},
    )

    FakeHandler()._route_api("POST", "/api/run/friend", {})

    assert spawned == [(False, "account1", "好友A", "revision-1", False)]
    assert replies == [(202, {"id": 1, "status": "running"})]


def test_logs_payload_includes_scoped_run_journal(monkeypatch, tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "run-journal.json").write_text(json.dumps([{
        "finished_at": "2026-10-04T20:00:00+08:00",
        "scope": "friend",
        "target": "好友A",
        "status": "success",
        "sent": 2,
    }], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web_server, "_account_artifacts_dir", lambda _account: artifacts)

    payload = web_server._logs_payload("account1")

    assert payload["runs"] == [{
        "finished_at": "2026-10-04T20:00:00+08:00",
        "scope": "friend",
        "target": "好友A",
        "status": "success",
        "sent": 2,
    }]

"""好友独立时间只用模拟配置与任务，不触发真实发送或 Windows 设置。"""
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.catchup as catchup
import web_server
from app.config import ConfigError, load_task
from app.schedule_slots import planned_slots, targets_for_slot
from tests.test_config import settings_for, write_config


def test_friend_times_parse_and_filter(tmp_path: Path) -> None:
    raw = {"send_time": "17:20", "targets": [
        {"name": "甲", "messages": [{"type": "text", "content": "一"}]},
        {"name": "乙", "send_time": "20:25", "messages": [{"type": "text", "content": "二"}]},
        {"name": "丙", "send_time": "20:25", "messages": [{"type": "text", "content": "三"}]},
    ]}
    task = load_task(settings_for(write_config(tmp_path, raw)))
    assert planned_slots(raw) == ["17:20", "20:25"]
    assert [t.name for t in targets_for_slot(task.targets, task.send_time, "17:20")] == ["甲"]
    assert [t.name for t in targets_for_slot(task.targets, task.send_time, "20:25")] == ["乙", "丙"]
    assert targets_for_slot(task.targets, task.send_time, "09:00") == ()


def test_global_and_friend_modes_are_exclusive(tmp_path: Path) -> None:
    raw = {"send_time": "17:20", "schedule_mode": "global", "targets": [
        {"name": "甲", "send_time": "18:25", "messages": [{"type": "text", "content": "一"}]},
        {"name": "乙", "send_time": "20:25", "schedule_enabled": False,
         "messages": [{"type": "text", "content": "二"}]},
    ]}
    path = write_config(tmp_path, raw)
    global_task = load_task(settings_for(path))
    assert planned_slots(raw) == ["17:20"]
    assert [t.name for t in targets_for_slot(global_task.targets, "17:20", "17:20", "global")] == ["甲", "乙"]
    assert targets_for_slot(global_task.targets, "17:20", "18:25", "global") == ()
    raw["schedule_mode"] = "friends"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    friend_task = load_task(settings_for(path))
    assert planned_slots(raw) == ["18:25"]
    assert [t.name for t in targets_for_slot(friend_task.targets, "17:20", "18:25", "friends")] == ["甲"]
    assert targets_for_slot(friend_task.targets, "17:20", "17:20", "friends") == ()


def test_invalid_friend_time_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"send_time": "17:20", "targets": [
        {"name": "甲", "send_time": "25:00", "messages": [{"type": "text", "content": "一"}]}
    ]})
    with pytest.raises(ConfigError, match="send_time"):
        load_task(settings_for(path))


def test_sync_creates_only_needed_slots_and_disables_stale(monkeypatch, tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"send_time": "17:20", "targets": [
        {"name": "甲", "send_time": "20:25"},
    ]}), encoding="utf-8")
    monkeypatch.setattr(web_server, "_account_task_path", lambda _: task)
    monkeypatch.setattr(web_server, "_schedule_manifest_path", lambda _: tmp_path / "slots.json")
    (tmp_path / "slots.json").write_text('{"custom_slots":["09:00"]}', encoding="utf-8")
    calls = []
    monkeypatch.setattr(web_server, "_sync_schedule", lambda aid, st, **kw: calls.append(("sync", st, kw)))
    monkeypatch.setattr(web_server, "_disable_task", lambda name: calls.append(("disable", name)))
    web_server._sync_account_schedules("account1")
    assert ("sync", "20:25", {"custom": True}) in calls
    assert ("disable", web_server._account_schedule_name("account1")) in calls
    assert ("disable", web_server._slot_task_name("account1", "09:00", "17:20")) in calls
    assert json.loads((tmp_path / "slots.json").read_text(encoding="utf-8")) == {"custom_slots": ["20:25"]}


def test_friend_save_syncs_and_returns_readback(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "task.json"
    path.write_text(json.dumps({"send_time": "17:20", "targets": [
        {"name": "甲", "messages": [{"type": "text", "content": "一"}]}
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web_server, "_account_task_path", lambda _: path)
    monkeypatch.setattr(web_server, "_account_enabled", lambda _: True)
    monkeypatch.setattr(web_server, "_effective_schedule", lambda _: {"mode": "windows"})
    synced = []
    monkeypatch.setattr(web_server, "_sync_account_schedules", lambda _: synced.append(
        json.loads(path.read_text(encoding="utf-8"))))
    monkeypatch.setattr(web_server, "_config_payload", lambda _: json.loads(path.read_text(encoding="utf-8")))
    replies = []
    handler = object.__new__(web_server.Handler)
    handler._send_json = lambda data, status=200: replies.append(data)
    handler._save_config({"schedule_mode": "friends", "targets": [
        {"name": "甲", "content_mode": "custom", "send_time": "18:25",
         "schedule_enabled": True, "messages": [{"type": "text", "content": "一"}]}
    ]}, "account1")
    assert synced[0]["targets"][0]["send_time"] == "18:25"
    assert synced[0]["schedule_mode"] == "friends"
    assert replies[0]["schedule"]["mode"] == "windows"
    assert "warn" not in replies[0]


def test_switching_to_friend_mode_preserves_existing_friend_time(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "task.json"
    path.write_text(json.dumps({"send_time": "17:20", "schedule_mode": "global", "targets": [
        {"name": "甲", "messages": [{"type": "text", "content": "一"}]},
        {"name": "乙", "send_time": "18:25", "messages": [{"type": "text", "content": "二"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web_server, "_account_task_path", lambda _: path)
    monkeypatch.setattr(web_server, "_account_enabled", lambda _: True)
    monkeypatch.setattr(web_server, "_effective_schedule", lambda _: {"mode": "windows"})
    synced = []
    monkeypatch.setattr(web_server, "_sync_account_schedules", lambda _: synced.append(
        json.loads(path.read_text(encoding="utf-8"))))
    replies = []
    handler = object.__new__(web_server.Handler)
    handler._send_json = lambda data, status=200: replies.append(data)
    handler._save_schedule({"time": "17:20", "mode": "friends"}, "account1")
    assert [t["send_time"] for t in synced[0]["targets"]] == ["17:20", "18:25"]
    assert planned_slots(synced[0]) == ["17:20", "18:25"]
    assert replies[0]["schedule"]["mode"] == "windows"


def test_catchup_blocks_mixed_friend_times(monkeypatch, tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text('{"send_time":"17:20","targets":[{"name":"甲","send_time":"20:25"}]}', encoding="utf-8")
    monkeypatch.setattr(catchup, "load_accounts", lambda _: [SimpleNamespace(
        id="a", enabled=True, env_file=tmp_path / ".env.a")])
    monkeypatch.setattr(catchup, "account_env", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(catchup, "load_settings", lambda _: SimpleNamespace(
        task_config_path=task, artifacts_dir=tmp_path / "artifacts" / "a"))
    never = lambda *a, **kw: pytest.fail("不得启动发送进程")
    now = datetime(2026, 10, 3, 21, tzinfo=timezone(timedelta(hours=8)))
    assert catchup.check_once(now, tmp_path, never) == {"a": "config_failed"}

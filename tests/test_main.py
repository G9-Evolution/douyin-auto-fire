from contextlib import asynccontextmanager
from types import SimpleNamespace
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.browser import AuthenticationError
from app.models import Message, Settings, Target, TargetResult, TaskConfig
import app.main as main_module


def _settings(tmp_path) -> Settings:
    return Settings(
        task_config_path=tmp_path / "config.json",
        storage_state=None,
        cookie="[]",
        headless=True,
        browser_path=None,
        artifacts_dir=tmp_path / "artifacts",
        trace=False,
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=token",
        dingtalk_secret="SEC-secret",
    )


def _task() -> TaskConfig:
    message = Message(type="text", content="测试")
    return TaskConfig(
        task_id="daily-streak",
        timezone="Asia/Shanghai",
        targets=(Target(name="好友A", messages=(message,)), Target(name="好友B", messages=(message,))),
        stickers={},
        interval_min=0,
        interval_max=0,
        continue_on_error=True,
        prevent_duplicates=False,
    )


@pytest.mark.asyncio
async def test_authentication_failure_stops_remaining_targets_and_notifies(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    task = _task()
    page = MagicMock()
    session = SimpleNamespace(page=page, context=MagicMock())

    @asynccontextmanager
    async def fake_open_douyin(_settings):
        yield session

    history = MagicMock()
    history.run_date.return_value = "2026-08-09"
    chat = MagicMock()
    chat.open_target = AsyncMock()
    notify = AsyncMock()
    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: task)
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", fake_open_douyin)
    monkeypatch.setattr(main_module, "open_private_messages", AsyncMock())
    monkeypatch.setattr(main_module, "DouyinChat", MagicMock(return_value=chat))
    monkeypatch.setattr(main_module, "verify_login", AsyncMock(side_effect=AuthenticationError("登录失效")))
    monkeypatch.setattr(main_module, "_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    monkeypatch.setattr(main_module, "_notify_dingtalk", notify)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _path, _aliases=None: None)

    with pytest.raises(AuthenticationError, match="登录失效"):
        await main_module.run()

    # 新的实现使用 _open_target_with_retry() 包装器，内部调用时 retries=0
    chat.open_target.assert_awaited_once_with("好友A", retries=0)
    results = notify.await_args.args[3]
    assert [(result.target, result.status) for result in results] == [("好友A", "failed")]


@pytest.mark.asyncio
async def test_browser_start_failure_still_notifies(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)

    @asynccontextmanager
    async def broken_open_douyin(_settings):
        raise RuntimeError("浏览器启动失败")
        yield

    history = MagicMock()
    history.run_date.return_value = "2026-08-09"
    notify = AsyncMock()
    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: _task())
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", broken_open_douyin)
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    monkeypatch.setattr(main_module, "_notify_dingtalk", notify)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _path, _aliases=None: None)

    with pytest.raises(RuntimeError, match="浏览器启动失败"):
        await main_module.run()

    results = notify.await_args.args[3]
    assert [(result.target, result.status) for result in results] == [("运行检查", "failed")]


@pytest.mark.asyncio
async def test_waits_between_consecutive_messages_for_same_friend(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    messages = (Message(type="text", content="一"), Message(type="text", content="二"))
    task = TaskConfig(
        task_id="daily-streak",
        timezone="Asia/Shanghai",
        targets=(Target(name="好友A", messages=messages),),
        stickers={},
        interval_min=0.5,
        interval_max=0.5,
        continue_on_error=True,
        prevent_duplicates=False,
    )
    page = MagicMock()
    session = SimpleNamespace(page=page, context=MagicMock())

    @asynccontextmanager
    async def fake_open_douyin(_settings):
        yield session

    history = MagicMock()
    history.run_date.return_value = "2026-08-09"
    chat = MagicMock()
    chat.open_target = AsyncMock()
    send_message = AsyncMock()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(main_module, "load_settings", lambda _env=None: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _settings: task)
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", fake_open_douyin)
    monkeypatch.setattr(main_module, "open_private_messages", AsyncMock())
    monkeypatch.setattr(main_module, "DouyinChat", MagicMock(return_value=chat))
    monkeypatch.setattr(main_module, "verify_login", AsyncMock())
    monkeypatch.setattr(main_module, "send_message", send_message)
    monkeypatch.setenv("DOUYIN_CATCHUP_DATE", datetime.now().astimezone().date().isoformat())
    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    monkeypatch.setattr(main_module, "_screenshot", AsyncMock(return_value=None))
    monkeypatch.setattr(main_module, "_write_results", MagicMock())
    notify = AsyncMock()
    monkeypatch.setattr(main_module, "_notify_dingtalk", notify)
    monkeypatch.setattr(main_module, "_configure_logging", lambda _path, _aliases=None: None)

    assert await main_module.run() == 0
    assert send_message.await_count == 2
    assert sleeps == [0.5]
    notify.assert_not_awaited()
    marker = json.loads((settings.artifacts_dir / "send-day.json").read_text(encoding="utf-8"))
    assert marker["status"] == "sent"  # 即使 prevent_duplicates=False 也要留下补跑判定依据


def test_write_results_appends_scoped_run_journal(tmp_path) -> None:
    main_module._write_results(
        tmp_path,
        "daily-streak",
        False,
        [TargetResult(target="好友A", status="success", sent=2)],
        source="manual",
        target_name="好友A",
    )

    journal = json.loads((tmp_path / "run-journal.json").read_text(encoding="utf-8"))
    assert journal[-1]["scope"] == "friend"
    assert journal[-1]["target"] == "好友A"
    assert journal[-1]["status"] == "success"
    assert journal[-1]["sent"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("second_time, expected", [("23:55", "好友A"), ("17:20", "好友A、好友B")])
async def test_scheduled_journal_names_only_due_friends(monkeypatch, tmp_path, second_time, expected):
    from dataclasses import replace
    import web_server

    settings = _settings(tmp_path)
    original = _task()
    task = replace(original, schedule_mode="friends", send_time="17:20", targets=(
        replace(original.targets[0], send_time="17:20"),
        replace(original.targets[1], send_time=second_time),
    ))
    session = SimpleNamespace(page=MagicMock(), context=MagicMock())

    @asynccontextmanager
    async def fake_open(_settings):
        yield session

    history = MagicMock()
    history.run_date.return_value = "2026-10-05"
    chat = MagicMock()
    chat.open_target = AsyncMock()
    send = AsyncMock()
    monkeypatch.setattr(main_module, "load_settings", lambda _: settings)
    monkeypatch.setattr(main_module, "load_task", lambda _: task)
    monkeypatch.setattr(main_module, "History", MagicMock(return_value=history))
    monkeypatch.setattr(main_module, "open_douyin", fake_open)
    monkeypatch.setattr(main_module, "open_private_messages", AsyncMock())
    monkeypatch.setattr(main_module, "verify_login", AsyncMock())
    monkeypatch.setattr(main_module, "DouyinChat", MagicMock(return_value=chat))
    monkeypatch.setattr(main_module, "send_message", send)
    monkeypatch.setattr(main_module, "_configure_logging", lambda *args: None)
    monkeypatch.setattr(main_module, "_notify_dingtalk", AsyncMock())
    monkeypatch.setattr(main_module, "_notify_webhook", AsyncMock())
    monkeypatch.setattr(web_server, "_account_artifacts_dir", lambda _: settings.artifacts_dir)

    assert await main_module.run(source="scheduled", schedule_slot="17:20") == 0
    called_names = [call.args[0] for call in chat.open_target.await_args_list]
    assert called_names == expected.split("、")
    assert send.await_count == len(called_names)
    journal = json.loads((settings.artifacts_dir / "run-journal.json").read_text(encoding="utf-8"))
    assert journal[-1]["scope"] == "scheduled"
    assert journal[-1]["target"] == expected
    assert journal[-1]["sent"] == len(called_names)
    assert web_server._logs_payload("fixture_account")["runs"][0]["target"] == expected


@pytest.mark.parametrize("source, dry_run, expected", [
    ("manual", False, "全部好友"),
    ("scheduled", False, "定时好友（对象未记录）"),
    ("scheduled", True, None),
])
def test_run_journal_preserves_global_and_dry_run_semantics(tmp_path, source, dry_run, expected):
    main_module._write_results(tmp_path, "fixture", dry_run,
        [TargetResult(target="好友A", status="success", sent=0 if dry_run else 1)], source=source)
    path = tmp_path / "run-journal.json"
    if dry_run:
        assert not path.exists()
    else:
        assert json.loads(path.read_text(encoding="utf-8"))[-1]["target"] == expected

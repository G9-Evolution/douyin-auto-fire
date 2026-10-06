"""错时补跑只用模拟时间和进程；绝不访问抖音或真实账号。"""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.catchup as catchup
from app.main import ensure_catchup_same_day
from app.config import ConfigError
from app.accounts import Account
from app.schedule_journal import delivery_evidence, read_status, write_status


CN = timezone(timedelta(hours=8))


def moment(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=CN)


def test_due_boundaries_and_cross_day(tmp_path: Path) -> None:
    assert catchup.due_reason(moment(1, 17, 29), "17:20", {}, {}, tmp_path / "history.json") == "not_due"
    assert catchup.due_reason(moment(1, 17, 30), "17:20", {}, {}, tmp_path / "history.json") == "due"
    old = {"date": "2026-09-30", "status": "starting"}
    assert catchup.due_reason(moment(1, 17, 30), "17:20", old, old, tmp_path / "history.json") == "due"
    assert catchup.due_reason(moment(2, 0, 5), "17:20", {}, {}, tmp_path / "history.json") == "not_due"
    assert catchup.due_reason(moment(1, 18), "17:20", {"status": "corrupt"}, {},
                              tmp_path / "history.json") == "journal_failed"


def test_normal_trigger_and_existing_send_skip(tmp_path: Path) -> None:
    write_status(tmp_path / "scheduled.json", "normal_triggered", now=moment(1, 17, 20))
    assert catchup.due_reason(moment(1, 18), "17:20", read_status(tmp_path / "scheduled.json"),
                              {}, tmp_path / "history.json") == "normal_pending"
    (tmp_path / "history.json").write_text(json.dumps({
        "task:2026-10-01:friend:msg": {"status": "unknown"}}), encoding="utf-8")
    assert catchup.due_reason(moment(1, 18), "17:20", {}, {}, tmp_path / "history.json") == "result_unknown"
    (tmp_path / "history.json").unlink()
    write_status(tmp_path / "send-day.json", "unknown_send", now=moment(1, 17, 25))
    assert catchup.due_reason(moment(1, 18), "17:20", {}, {}, tmp_path / "history.json") == "result_unknown"


def test_explicit_normal_failure_can_retry_once_only_without_send_evidence(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    failed = {"date": "2026-10-01", "status": "normal_failed"}
    finished_without_send = {"date": "2026-10-01", "status": "normal_finished"}
    for scheduled in (failed, finished_without_send, {"date": "2026-10-01", "status": "login_failed"}):
        assert catchup.due_reason(moment(1, 18), "17:20", scheduled, {}, history) == "due"
    assert catchup.due_reason(moment(1, 18), "17:20", failed,
                              {"date": "2026-10-01", "status": "process_failed"}, history) == "already_checked"
    write_status(tmp_path / "send-day.json", "sent", now=moment(1, 17, 25))
    assert catchup.due_reason(moment(1, 18), "17:20", failed, {}, history) == "already_sent"


def test_partial_or_ambiguous_send_never_retries(tmp_path: Path) -> None:
    history = tmp_path / "history.json"
    failed = {"date": "2026-10-01", "status": "normal_failed"}
    history.write_text(json.dumps({
        "task:2026-10-01:friend:one": {"status": "success"},
        "task:2026-10-01:friend:two": {"status": "unknown"},
    }), encoding="utf-8")
    assert delivery_evidence(history, "2026-10-01") == "unknown"
    assert catchup.due_reason(moment(1, 18), "17:20", failed, {}, history) == "result_unknown"
    history.write_text("{broken", encoding="utf-8")
    assert catchup.due_reason(moment(1, 18), "17:20", failed, {}, history) == "result_unknown"


def test_result_file_send_is_evidence_even_without_history(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text(json.dumps({
        "finished_at": moment(1, 17, 25).isoformat(), "dry_run": False,
        "results": [{"status": "success", "sent": 1}],
    }), encoding="utf-8")
    assert delivery_evidence(tmp_path / "history.json", "2026-10-01") == "sent"
    assert delivery_evidence(tmp_path / "history.json", "2026-10-02") == "none"


def test_two_accounts_independent_and_repeat_is_blocked(monkeypatch, tmp_path: Path) -> None:
    accounts = [Account(aid, True, tmp_path / f".env.{aid}") for aid in ("a", "b")]
    for aid, send_time in (("a", "17:20"), ("b", "20:25")):
        task = tmp_path / "config" / "tasks" / f"{aid}.json"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(json.dumps({"send_time": send_time}), encoding="utf-8")
    monkeypatch.setattr(catchup, "load_accounts", lambda _: accounts)
    monkeypatch.setattr(catchup, "account_env", lambda *args, **kwargs: nullcontext())
    current = iter(("a", "b", "a", "b"))
    monkeypatch.setattr(catchup, "load_settings", lambda _: (lambda aid: SimpleNamespace(
        task_config_path=tmp_path / "config" / "tasks" / f"{aid}.json",
        artifacts_dir=tmp_path / "artifacts" / aid))(next(current)))
    monkeypatch.setattr(catchup, "validation_status", lambda *args: {"status": "passed"})
    calls = []

    def fake_invoke(args, **kwargs):
        aid = args[-1]
        calls.append(aid)
        artifacts = tmp_path / "artifacts" / aid
        (artifacts / "result.json").write_text(json.dumps({
            "finished_at": moment(1, 19).isoformat(), "dry_run": False,
            "results": [{"sent": 1}]}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert catchup.check_once(moment(1, 18), tmp_path, fake_invoke) == {"a": "sent", "b": "not_due"}
    assert catchup.check_once(moment(1, 18, 15), tmp_path, fake_invoke) == {"a": "already_checked", "b": "not_due"}
    assert calls == ["a"]


def test_new_account_retries_explicit_failure_and_reports_unknown(monkeypatch, tmp_path: Path) -> None:
    account_id = "future_account"
    task = tmp_path / "config" / "tasks" / f"{account_id}.json"
    task.parent.mkdir(parents=True)
    task.write_text('{"send_time":"17:20"}', encoding="utf-8")
    artifacts = tmp_path / "artifacts" / account_id
    write_status(artifacts / "scheduled.json", "normal_failed", now=moment(1, 17, 25))
    monkeypatch.setattr(catchup, "load_accounts", lambda _: [Account(account_id, True, tmp_path / ".env.future")])
    monkeypatch.setattr(catchup, "account_env", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(catchup, "load_settings", lambda _: SimpleNamespace(
        task_config_path=task, artifacts_dir=artifacts))
    monkeypatch.setattr(catchup, "validation_status", lambda *args: {"status": "passed"})
    calls = []

    def ambiguous_invoke(args, **kwargs):
        calls.append(args[-1])
        write_status(artifacts / "send-day.json", "unknown_send", now=moment(1, 18))
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    assert catchup.check_once(moment(1, 18), tmp_path, ambiguous_invoke) == {account_id: "result_unknown"}
    assert catchup.check_once(moment(1, 18, 15), tmp_path, ambiguous_invoke) == {account_id: "already_checked"}
    assert calls == [account_id]


def test_validation_failure_never_starts_and_can_retry_after_validation(monkeypatch, tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text('{"send_time":"17:20"}', encoding="utf-8")
    monkeypatch.setattr(catchup, "load_accounts", lambda _: [Account("a", True, tmp_path / ".env.a")])
    monkeypatch.setattr(catchup, "account_env", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(catchup, "load_settings", lambda _: SimpleNamespace(
        task_config_path=task, artifacts_dir=tmp_path / "artifacts" / "a"))
    monkeypatch.setattr(catchup, "validation_status", lambda *args: {"status": "pending"})
    never = lambda *args, **kwargs: pytest.fail("不得启动发送进程")
    assert catchup.check_once(moment(1, 18), tmp_path, never) == {"a": "validation_failed"}
    assert read_status(tmp_path / "artifacts" / "a" / "catchup.json")["status"] == "validation_failed"
    assert catchup.due_reason(moment(1, 18, 15), "17:20", {},
                              read_status(tmp_path / "artifacts" / "a" / "catchup.json"),
                              tmp_path / "missing-history.json") == "due"


def test_login_and_process_start_failures_are_reported(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    assert catchup._classify_result(artifacts, moment(1, 18).isoformat(), 1,
                                    "AuthenticationError", "2026-10-01")[0] == "login_failed"
    assert catchup._classify_result(artifacts, moment(1, 18).isoformat(), 1,
                                    "other failure", "2026-10-01")[0] == "process_failed"
    assert catchup._classify_result(artifacts, moment(1, 18).isoformat(), 1,
                                    "CATCHUP_EXPIRED", "2026-10-01")[0] == "expired"


def test_catchup_never_sends_after_midnight() -> None:
    ensure_catchup_same_day("2026-10-01", moment(1, 23, 59))
    with pytest.raises(ConfigError, match="CATCHUP_EXPIRED"):
        ensure_catchup_same_day("2026-10-01", moment(2, 0, 0))


def test_process_start_failure_is_once_per_day(monkeypatch, tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text('{"send_time":"17:20"}', encoding="utf-8")
    monkeypatch.setattr(catchup, "load_accounts", lambda _: [Account("a", True, tmp_path / ".env.a")])
    monkeypatch.setattr(catchup, "account_env", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(catchup, "load_settings", lambda _: SimpleNamespace(
        task_config_path=task, artifacts_dir=tmp_path / "artifacts" / "a"))
    monkeypatch.setattr(catchup, "validation_status", lambda *args: {"status": "passed"})
    calls = []

    def cannot_start(*args, **kwargs):
        calls.append(1)
        raise OSError("mocked")

    assert catchup.check_once(moment(1, 18), tmp_path, cannot_start) == {"a": "process_start_failed"}
    assert catchup.check_once(moment(1, 18, 15), tmp_path, cannot_start) == {"a": "already_checked"}
    assert calls == [1]

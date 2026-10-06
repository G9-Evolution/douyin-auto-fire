"""配置试运行凭据只对同一账号、配置和代码版本有效。"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import ConfigError
from app.validation import record_validation, require_validation, validation_status
import pytest

import app.main as main_module
from types import SimpleNamespace


def _root(tmp_path: Path) -> tuple[Path, Path]:
    for relative in ("VERSION.txt", "run.py", "web_server.py", "web/index.html", "requirements.lock.txt", "app/main.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    accounts = tmp_path / "config" / "accounts.json"
    accounts.parent.mkdir(parents=True, exist_ok=True)
    accounts.write_text(json.dumps({"accounts": [{"id": "account1", "enabled": True, "env_file": ".env.account1"}]}), encoding="utf-8")
    (tmp_path / ".env.account1").write_text("DOUYIN_STORAGE_STATE=storage-state-account1.json\n", encoding="utf-8")
    (tmp_path / "storage-state-account1.json").write_text('{"cookies": []}', encoding="utf-8")
    task = tmp_path / "config" / "tasks" / "account1.json"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text('{"send_time":"17:20"}', encoding="utf-8")
    return tmp_path, task


def test_successful_dry_run_receipt_tracks_config_and_code(tmp_path: Path):
    root, task = _root(tmp_path)
    artifacts = root / "artifacts" / "account1"
    assert validation_status("account1", task, artifacts, root)["status"] == "pending"
    record_validation("account1", task, artifacts, root)
    assert validation_status("account1", task, artifacts, root)["status"] == "passed"
    assert validation_status("account2", task, artifacts, root)["status"] == "pending"

    task.write_text('{"send_time":"18:00"}', encoding="utf-8")
    assert validation_status("account1", task, artifacts, root)["status"] == "pending"
    record_validation("account1", task, artifacts, root)
    (root / "app" / "main.py").write_text("new version", encoding="utf-8")
    assert validation_status("account1", task, artifacts, root)["status"] == "pending"


def test_account_change_invalidates_receipt(tmp_path: Path):
    root, task = _root(tmp_path)
    artifacts = root / "artifacts" / "account1"
    record_validation("account1", task, artifacts, root)
    (root / "config" / "accounts.json").write_text(
        json.dumps({"accounts": [{"id": "account1", "enabled": False, "env_file": ".env.account1"}]}),
        encoding="utf-8",
    )
    assert validation_status("account1", task, artifacts, root)["status"] == "pending"


def test_ui_and_other_accounts_do_not_invalidate_receipt(tmp_path: Path):
    root, task = _root(tmp_path)
    artifacts = root / "artifacts" / "account1"
    record_validation("account1", task, artifacts, root)
    (root / "web" / "index.html").write_text("new layout", encoding="utf-8")
    (root / "web_server.py").write_text("new ui route", encoding="utf-8")
    (root / "VERSION.txt").write_text("new version", encoding="utf-8")
    accounts = root / "config" / "accounts.json"
    accounts.write_text(json.dumps({"default_id": "account2", "accounts": [
        {"id": "account1", "label": "renamed", "enabled": True, "env_file": ".env.account1"},
        {"id": "account2", "label": "new", "enabled": False, "env_file": ".env.account2"},
    ]}), encoding="utf-8")
    assert validation_status("account1", task, artifacts, root)["status"] == "passed"
    assert validation_status("account2", root / "config/tasks/account2.json", root / "artifacts/account2", root)["status"] == "pending"


def test_login_change_invalidates_receipt(tmp_path: Path):
    root, task = _root(tmp_path)
    artifacts = root / "artifacts" / "account1"
    record_validation("account1", task, artifacts, root)
    (root / "storage-state-account1.json").write_text('{"cookies": ["updated"]}', encoding="utf-8")
    assert validation_status("account1", task, artifacts, root)["status"] == "pending"


def test_formal_send_requires_matching_dry_run_receipt(tmp_path: Path):
    root, task = _root(tmp_path)
    artifacts = root / "artifacts" / "account1"
    with pytest.raises(ConfigError, match="无发送试运行"):
        require_validation("account1", task, artifacts, root)
    record_validation("account1", task, artifacts, root)
    require_validation("account1", task, artifacts, root)
    task.write_text('{"send_time":"20:25"}', encoding="utf-8")
    with pytest.raises(ConfigError, match="无发送试运行"):
        require_validation("account1", task, artifacts, root)


def test_single_account_cli_blocks_before_run(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(main_module, "_parse_cli_args", lambda: SimpleNamespace(dry_run=False, env_file=None))
    monkeypatch.setattr(main_module, "load_settings", lambda _: SimpleNamespace(
        task_config_path=tmp_path / "task.json", artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(main_module, "require_validation", lambda *args: (_ for _ in ()).throw(
        ConfigError("未通过无发送试运行")))
    monkeypatch.setattr(main_module.asyncio, "run", lambda coro: pytest.fail("不应启动发送"))
    assert main_module.main() == 2

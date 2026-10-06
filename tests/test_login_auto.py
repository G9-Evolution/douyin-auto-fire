from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _login_module():
    path = Path(__file__).parents[1] / "scripts" / "login_auto.py"
    spec = importlib.util.spec_from_file_location("login_auto_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_save_account_avatar_only_accepts_http_urls(monkeypatch, tmp_path: Path) -> None:
    login_auto = _login_module()
    config = tmp_path / "config"
    config.mkdir()
    accounts_file = config / "accounts.json"
    accounts_file.write_text(json.dumps({"accounts": [{"id": "account1"}]}), encoding="utf-8")
    monkeypatch.setattr(login_auto, "PROJECT_ROOT", tmp_path)

    login_auto._save_account_avatar("account1", "https://cdn.example/avatar.webp")
    saved = json.loads(accounts_file.read_text(encoding="utf-8"))
    assert saved["accounts"][0]["avatar_url"] == "https://cdn.example/avatar.webp"

    login_auto._save_account_avatar("account1", "file:///private/avatar.png")
    assert json.loads(accounts_file.read_text(encoding="utf-8")) == saved

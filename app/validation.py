"""将无发送试运行结果绑定到具体配置与代码版本。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values

from app.config import ConfigError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fingerprint(account_id: str, task_path: Path, root: Path = PROJECT_ROOT) -> str:
    digest = hashlib.sha256()
    digest.update(b"send-validation-v2")
    digest.update(account_id.encode("utf-8"))
    digest.update(task_path.read_bytes())

    accounts_path = root / "config" / "accounts.json"
    env_file = root / ".env"
    if accounts_path.is_file():
        accounts = json.loads(accounts_path.read_text(encoding="utf-8"))["accounts"]
        account = next((item for item in accounts if item.get("id") == account_id), None)
        if account is None:
            raise ValueError(f"账号不存在: {account_id}")
        # 其他账号、默认账号和界面备注的变化不影响当前账号的发送验证。
        relevant_account = {key: account.get(key) for key in ("id", "enabled", "env_file")}
        digest.update(json.dumps(relevant_account, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        env_file = root / str(account.get("env_file") or f".env.{account_id}")

    env = dotenv_values(env_file) if env_file.is_file() else {}
    auth_settings = {key: env.get(key) for key in
                     ("DOUYIN_STORAGE_STATE", "DOUYIN_COOKIE", "TASK_CONFIG", "HEADLESS", "BROWSER_PATH")}
    digest.update(json.dumps(auth_settings, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    state_value = auth_settings["DOUYIN_STORAGE_STATE"]
    if state_value:
        state_path = Path(state_value).expanduser()
        if not state_path.is_absolute():
            state_path = root / state_path
        if state_path.is_file():
            digest.update(state_path.read_bytes())

    # 只绑定运行代码和依赖；工作台界面、服务路由和版本文案不影响发送逻辑。
    source_paths = [root / name for name in ("run.py", "requirements.lock.txt")]
    source_paths.extend(sorted((root / "app").glob("*.py")))
    for path in source_paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def validation_status(account_id: str, task_path: Path, artifacts_dir: Path, root: Path = PROJECT_ROOT) -> dict:
    """只读返回当前配置是否有对应的成功试运行凭据。"""
    receipt_path = artifacts_dir / "validation.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        valid = (receipt.get("account_id") == account_id
                 and receipt.get("fingerprint") == _fingerprint(account_id, task_path, root))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        valid = False
        receipt = {}
    return {"status": "passed" if valid else "pending",
            "validated_at": receipt.get("validated_at") if valid else None}


def require_validation(account_id: str, task_path: Path, artifacts_dir: Path,
                       root: Path = PROJECT_ROOT) -> None:
    """正式运行前检查当前账号、任务配置和代码的无发送试运行凭据。"""
    if validation_status(account_id, task_path, artifacts_dir, root)["status"] != "passed":
        raise ConfigError(f"账号 {account_id} 的当前配置或代码尚未通过无发送试运行；请先运行 --dry-run")


def record_validation(account_id: str, task_path: Path, artifacts_dir: Path, root: Path = PROJECT_ROOT) -> dict:
    """仅在该账号无发送试运行成功后调用；用同目录原子替换留存凭据。"""
    receipt = {
        "account_id": account_id,
        "validated_at": datetime.now().astimezone().isoformat(),
        "fingerprint": _fingerprint(account_id, task_path, root),
    }
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=artifacts_dir,
                                     prefix=".validation-", suffix=".json", delete=False) as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
        candidate = Path(stream.name)
    try:
        os.replace(candidate, artifacts_dir / "validation.json")
    finally:
        candidate.unlink(missing_ok=True)
    return receipt

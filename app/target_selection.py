"""Select one saved friend without changing the account's history or validation scope."""
from dataclasses import replace
import hashlib
from pathlib import Path

from app.config import ConfigError


def task_revision(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigError("无法读取当前好友配置，请刷新后重试") from exc


def require_revision(path: Path, expected: str) -> None:
    if not expected or task_revision(path) != expected:
        raise ConfigError("好友或发送内容已变更，请刷新后重新确认")


def select_target(task, name: str):
    matches = [target for target in task.targets if target.name == name]
    if not isinstance(name, str) or not name.strip() or len(matches) != 1:
        raise ConfigError("当前账号下未找到唯一的指定好友，已停止发送")
    return replace(task, targets=tuple(matches))

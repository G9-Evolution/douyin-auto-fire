"""好友独立发送时间的解析与分组。"""
from __future__ import annotations

import re


def normalize_send_time(value: object, label: str) -> str:
    """返回规范的 HH:MM，拒绝无效时间。"""
    from app.config import ConfigError
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", value.strip()):
        raise ConfigError(f"{label} 应为 HH:MM，例如 17:20")
    hour, minute = value.strip().split(":")
    return f"{int(hour):02d}:{minute}"


def planned_slots(raw: dict) -> list[str]:
    """当前任务文件中需要创建的每日触发时间。"""
    default = raw.get("send_time")
    if not default:
        return []
    default = normalize_send_time(default, "账号默认发送时间")
    targets = raw.get("targets")
    if targets is None:
        targets = [{"name": name} for name in raw.get("friends", [])]
    mode = raw.get("schedule_mode", "legacy")
    if not targets:
        return [] if mode == "friends" else [default]
    if mode == "global":
        return [default]
    if mode == "friends":
        return sorted({normalize_send_time(t.get("send_time"), "好友发送时间")
                       for t in targets if t.get("schedule_enabled", True)})
    return sorted({normalize_send_time(t.get("send_time"), "好友发送时间")
                   if t.get("send_time") else default for t in targets})


def targets_for_slot(targets: tuple, default_time: str, slot: str,
                     mode: str = "legacy") -> tuple:
    """只选中该计划时间需要处理的好友。"""
    if mode == "global":
        return targets if slot == default_time else ()
    if mode == "friends":
        return tuple(target for target in targets
                     if target.schedule_enabled and target.send_time == slot)
    return tuple(target for target in targets
                 if (target.send_time or default_time) == slot)

# -*- coding: utf-8 -*-
"""自动续火花 · 本地 Web 控制台（正式版初版）

启动：在项目根目录运行
    .\\.venv\\Scripts\\python.exe web_server.py
然后浏览器访问 http://127.0.0.1:8734

只监听本机回环地址，不对外开放。所有写操作直接作用于项目真实文件
（config.json / .env / config/accounts.json / Windows 计划任务），
运行/试运行通过子进程调用现有 run.py 完成，隔离于本服务进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from dotenv import dotenv_values, load_dotenv  # noqa: E402

from app.config import ConfigError, load_settings, load_task, normalize_douyin_id  # noqa: E402
from app.validation import require_validation, validation_status  # noqa: E402
from app.target_selection import require_revision, task_revision  # noqa: E402
from app.schedule_journal import read_status  # noqa: E402
from app.schedule_slots import normalize_send_time, planned_slots  # noqa: E402

PORT = int(os.getenv("DOUYIN_WEB_PORT", "8734"))
APP_VERSION = (PROJECT_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
TASK_NAME = "CodexDouyinFire"
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

# 运行中的子进程任务（用于轮询）
_run_tasks: dict[int, dict[str, Any]] = {}
_run_seq = 0
_lock = threading.Lock()


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _mask(value: str | None, keep: int = 8) -> str | None:
    """脱敏：只保留前缀 keep 个字符，其余用 * 代替。"""
    if not value:
        return None
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


# --------------------------------------------------------------------------
# 多账号：账号 → 路径/任务名 映射
# --------------------------------------------------------------------------

def _account_env_file(account_id: str | None) -> Path | None:
    """账号对应的 env 文件。default/None 使用根 .env。"""
    if not account_id or account_id == "default":
        return None
    return PROJECT_ROOT / f".env.{account_id}"


def _account_task_path(account_id: str | None) -> Path:
    """账号对应的任务配置文件。default 用 config.json，其余用 config/tasks/account<id>.json。"""
    if not account_id or account_id == "default":
        return PROJECT_ROOT / "config.json"
    return PROJECT_ROOT / "config" / "tasks" / f"{account_id}.json"


def _account_artifacts_dir(account_id: str | None) -> Path:
    """账号对应的产物目录（history/result/metrics/log）。default 用根 artifacts/。"""
    if not account_id or account_id == "default":
        return PROJECT_ROOT / "artifacts"
    return PROJECT_ROOT / "artifacts" / account_id


def _account_schedule_name(account_id: str | None) -> str:
    """账号对应的计划任务名。default 用 CodexDouyinFire。"""
    if not account_id or account_id == "default":
        return TASK_NAME
    return f"{TASK_NAME}-{account_id}"


def _account_defaults_for_env(account_id: str | None) -> dict[str, str]:
    """新建/迁移账号时写入 env 的默认值（多账号独立任务文件）。"""
    if not account_id or account_id == "default":
        return {}
    return {"TASK_CONFIG": f"config/tasks/{account_id}.json"}


def _quote_env_value(value: str) -> str:
    """把环境变量值写成 .env 单行（必要时加双引号）。"""
    if any(ch in value for ch in ' "#\'\\'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _update_env_file(updates: dict[str, str | None]) -> None:
    """更新 .env 中的键。值为 None 表示删除该键。保留其他行与注释。"""
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    keys = set(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in keys:
            if updates[key] is not None:
                out.append(f"{key}={_quote_env_value(updates[key])}")
            keys.discard(key)
        else:
            out.append(line)
    for key in list(keys):
        if updates[key] is not None:
            out.append(f"{key}={_quote_env_value(updates[key])}")
    if out and out[-1] != "":
        out.append("")
    env_path.write_text("\n".join(out), encoding="utf-8")


def _read_env() -> dict[str, str]:
    return {k: v for k, v in (dotenv_values(PROJECT_ROOT / ".env") or {}).items() if v is not None}


def _message_to_dict(message: Any) -> dict:
    if message.type == "text":
        return {"type": "text", "content": message.content}
    if message.type == "image":
        return {"type": "image", "path": str(message.path)}
    if message.type == "douyin_sticker":
        return {"type": "douyin_sticker", "sticker": message.sticker}
    if message.type == "random":
        return {"type": "random", "choices": [_message_to_dict(c) for c in message.choices]}
    return {"type": message.type, "value": getattr(message, "content", None)}


def _parse_task_raw(raw: dict) -> dict:
    """把任务文件原始 JSON 转成统一 targets/global_messages 结构。"""
    targets_raw = raw.get("targets")
    if targets_raw is None and "friends" in raw:
        friends = raw.get("friends") or []
        messages = raw.get("messages") or []
        targets_raw = [{"name": str(f), "messages": messages} for f in friends]
    targets_raw = targets_raw or []
    targets = []
    for t in targets_raw:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        messages = [_message_to_dict(m) for m in _parse_messages(t.get("messages") or [])]
        targets.append({
            "name": str(t["name"]),
            "douyin_id": t.get("douyin_id"),
            "messages": messages,
            "content_mode": t.get("content_mode"),
            "send_time": t.get("send_time"),
            "schedule_enabled": t.get("schedule_enabled", True),
        })
    interval = raw.get("send_interval_seconds") or {}
    global_messages = [_message_to_dict(m) for m in _parse_messages(raw.get("global_messages") or [])]
    if not global_messages:
        global_messages = targets[0]["messages"] if targets else []
    for target in targets:
        if target["content_mode"] not in ("global", "custom"):
            target["content_mode"] = "global" if target["messages"] == global_messages else "custom"
    format_kind = "legacy" if "targets" not in raw else "targets"
    return {
        "format": format_kind,
        "targets": targets,
        "global_messages": global_messages,
        "send_interval": {"min": float(interval.get("min", 3)), "max": float(interval.get("max", 8))},
        "prevent_duplicates": bool(raw.get("prevent_duplicates", False)),
        "stickers": raw.get("stickers", {}),
    }


def _parse_messages(items: list) -> list[Any]:
    """把任务文件里的消息原始 dict 转成模型对象（供 _message_to_dict）。"""
    from app.models import Message
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mtype = item.get("type", "text")
        if mtype == "text":
            out.append(Message(type="text", content=item.get("value", item.get("content", ""))))
        elif mtype == "image":
            out.append(Message(type="image", path=str(item.get("path", ""))))
        elif mtype == "douyin_sticker":
            out.append(Message(type="douyin_sticker", sticker=item.get("sticker", "")))
        elif mtype == "random":
            choices = _parse_messages(item.get("choices") or [])
            out.append(Message(type="random", choices=tuple(choices)))
    return out


def _config_payload(account_id: str | None = None) -> dict:
    """bootstrap 用：解析后的 targets + 原始配置（按账号独立任务文件）。"""
    task_path = _account_task_path(account_id)
    revision = task_revision(task_path) if task_path.is_file() else None
    raw = _read_json(task_path) or {}
    if revision is not None:
        require_revision(task_path, revision)
    parsed = _parse_task_raw(raw)
    payload = {
        "task_id": raw.get("task_id", "daily-streak"),
        "task_config": str(task_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "account_id": account_id or "default",
        "format": parsed["format"],
        "targets": parsed["targets"],
        "global_messages": parsed["global_messages"],
        "send_interval": parsed["send_interval"],
        "prevent_duplicates": parsed["prevent_duplicates"],
        "stickers": parsed["stickers"],
        "schedule_mode": raw.get("schedule_mode", "legacy"),
        "friend_douyin_id": True,
        "friend_send": True,
        "task_revision": revision,
        "headless": False,
        "trace": True,
    }
    return payload


def _account_payload() -> dict:
    """账号信息：单账号模式 / 多账号模式。"""
    accounts_file = PROJECT_ROOT / "config" / "accounts.json"
    env = _read_env()
    if not accounts_file.is_file():
        # 单账号模式
        state_file = PROJECT_ROOT / "storage-state.json"
        return {
            "mode": "single",
            "default_id": None,
            "accounts": [
                {
                    "id": "default",
                    "label": "默认账号",
                    "enabled": True,
                    "is_default": True,
                    "logged_in": state_file.is_file(),
                    "storage_state": "storage-state.json" if state_file.is_file() else None,
                    "storage_mtime": datetime.fromtimestamp(state_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if state_file.is_file() else None,
                    "cookie": bool(env.get("DOUYIN_COOKIE")),
                    "task_config": "config.json",
                    "schedule_name": _account_schedule_name("default"),
                    "validation_status": validation_status("default", _account_task_path(None),
                                                           _account_artifacts_dir(None), PROJECT_ROOT)["status"],
                }
            ],
        }
    raw = _read_json(accounts_file) or {}
    accounts_raw = raw.get("accounts", [])
    default_id = raw.get("default_id")
    accounts = []
    for index, item in enumerate(accounts_raw):
        account_id = item.get("id", "")
        env_file = PROJECT_ROOT / str(item.get("env_file", f".env.account{account_id}"))
        aenv = {k: v for k, v in (dotenv_values(env_file) or {}).items() if v is not None}
        state = aenv.get("DOUYIN_STORAGE_STATE")
        state_path = PROJECT_ROOT / state if state else None
        is_default = (account_id == default_id) or (default_id is None and index == 0)
        accounts.append({
            "id": account_id,
            "label": item.get("label") or account_id,
            "enabled": bool(item.get("enabled", True)),
            "is_default": (account_id == default_id) or (default_id is None and item is accounts_raw[0]),
            "logged_in": bool(state_path and state_path.is_file()),
            "storage_state": state,
            "storage_mtime": datetime.fromtimestamp(state_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if state_path and state_path.is_file() else None,
            "cookie": bool(aenv.get("DOUYIN_COOKIE")),
            "avatar_url": item.get("avatar_url") if isinstance(item.get("avatar_url"), str) and item.get("avatar_url", "").startswith(("http://", "https://")) else None,
            "task_config": str(_account_task_path(account_id).relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "schedule_name": _account_schedule_name(account_id),
            "validation_status": validation_status(account_id, _account_task_path(account_id),
                                                   _account_artifacts_dir(account_id), PROJECT_ROOT)["status"],
        })
    return {"mode": "multi", "default_id": default_id, "accounts": accounts}


def _next_account_id(accounts_file: Path) -> str:
    """生成不重复的账号 id：account<序号>。

    取现有最大序号 + 1，不复用被删除的 id——否则新账号会撞上残留的
    config/tasks/<id>.json 旧数据（踩坑日志 BUG-10）。
    """
    raw = _read_json(accounts_file) or {}
    max_seq = 0
    for item in raw.get("accounts", []):
        if isinstance(item, dict):
            aid = str(item.get("id") or "")
            if aid.startswith("account") and aid[7:].isdigit():
                max_seq = max(max_seq, int(aid[7:]))
    return f"account{max_seq + 1}"


def _add_account(label: str) -> dict:
    """添加一个新账号。

    当前是单账号模式时，先迁移为多账号：把现有凭证（storage-state.json +
    根 .env）登记为「默认账号」条目（env_file=.env），再追加新账号。
    返回新的账号信息 payload。
    """
    accounts_file = PROJECT_ROOT / "config" / "accounts.json"
    accounts: list[dict] = []
    default_id: str | None = None
    if accounts_file.is_file():
        raw = _read_json(accounts_file) or {}
        accounts = [dict(a) for a in raw.get("accounts", []) if isinstance(a, dict)]
        default_id = raw.get("default_id")
        for a in accounts:
            if "label" not in a or not a.get("label"):
                a.setdefault("label", a.get("id"))
    else:
        # 单账号 → 迁移默认账号
        env = _read_env()
        if "DOUYIN_STORAGE_STATE" not in env:
            _update_env_file({"DOUYIN_STORAGE_STATE": "storage-state.json"})
        accounts.append({
            "id": "default",
            "label": "默认账号",
            "enabled": True,
            "env_file": ".env",
        })
        default_id = "default"

    account_id = _next_account_id(accounts_file)
    env_file = PROJECT_ROOT / f".env.{account_id}"
    state_file = f"storage-state-{account_id}.json"
    # 新账号 env：独立凭证路径 + 独立任务文件 + 无头默认值（与根 .env 一致的基线）
    env_lines = [
        "HEADLESS=true",
        f"DOUYIN_STORAGE_STATE={state_file}",
        f"TASK_CONFIG=config/tasks/{account_id}.json",
    ]
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    # 初始化独立任务文件：新账号好友/内容为空；若 id 撞上残留文件则重置为空模板（BUG-10）
    task_path = PROJECT_ROOT / "config" / "tasks" / f"{account_id}.json"
    _write_json(task_path, {"friends": [], "messages": []})
    accounts.append({
        "id": account_id,
        "label": label or account_id,
        "enabled": False,
        "env_file": env_file.name,
    })
    out: dict[str, Any] = {"accounts": accounts}
    if default_id:
        out["default_id"] = default_id
    _write_json(accounts_file, out)
    return _account_payload()


def _remove_account(account_id: str) -> dict:
    """删除账号条目。删除后为空时回退单账号模式；default 被删时重新指定默认。"""
    accounts_file = PROJECT_ROOT / "config" / "accounts.json"
    if not accounts_file.is_file():
        raise ConfigError("当前为单账号模式，无需删除账号")
    raw = _read_json(accounts_file) or {}
    accounts = [dict(a) for a in raw.get("accounts", []) if isinstance(a, dict)]
    remaining = [a for a in accounts if a.get("id") != account_id]
    if len(remaining) == len(accounts):
        raise ConfigError(f"账号 {account_id} 不存在")
    _disable_schedule(account_id)

    if not remaining:
        # 全部删光 → 回退单账号模式（保留根 .env 与 storage-state.json 凭证）
        accounts_file.unlink(missing_ok=True)
        return _account_payload()

    # 删除该账号的独立 env 文件（default 账号的根 .env 不删）
    if account_id != "default":
        env_file = PROJECT_ROOT / f".env.{account_id}"
        env_file.unlink(missing_ok=True)

    default_id = raw.get("default_id")
    if default_id == account_id:
        default_id = remaining[0].get("id")
    out: dict[str, Any] = {"accounts": remaining}
    if default_id:
        out["default_id"] = default_id
    _write_json(accounts_file, out)
    return _account_payload()


def _notify_payload() -> dict:
    env = _read_env()
    dw = env.get("DINGTALK_WEBHOOK")
    ds = env.get("DINGTALK_SECRET")
    wu = env.get("WEBHOOK_URL")
    return {
        "dingtalk_enabled": bool(dw and ds),
        "dingtalk_webhook": _mask(dw),
        "dingtalk_webhook_configured": bool(dw),
        "dingtalk_secret_configured": bool(ds),
        "webhook_enabled": bool(wu),
        "webhook_url": _mask(wu),
        "webhook_url_configured": bool(wu),
        "webhook_headers_configured": bool(env.get("WEBHOOK_HEADERS")),
        "webhook_template_configured": bool(env.get("WEBHOOK_TEMPLATE")),
    }


def _schtasks_query(task_name: str | None = None) -> dict:
    """查询计划任务状态（只读）。task_name 缺省用默认任务名。"""
    task_name = task_name or TASK_NAME
    try:
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", task_name, "/v", "/fo", "LIST"],
            capture_output=True, timeout=15,
        )
    except Exception as exc:
        return {"task_exists": False, "task_name": task_name, "error": f"查询失败: {exc}"}
    if proc.returncode != 0:
        return {"task_exists": False, "task_name": task_name,
                "error": _decode_command_output(proc.stderr or proc.stdout).strip()}
    text = _decode_command_output(proc.stdout)
    def _find(label_en: str, label_zh: str) -> str | None:
        # 行首锚定，避免中文输出中「登录状态/登录模式」误匹配；优先英文标签
        m = re.search(rf"(?m)^\s*{label_en}\s*:\s*(.+)", text)
        if m:
            return m.group(1).strip()
        m = re.search(rf"(?m)^\s*{label_zh}\s*:\s*(.+)", text)
        return m.group(1).strip() if m else None
    next_run = _find("Next Run Time", "下次运行时间")
    status = _find("Status", "模式")
    start_in = _find("Start In", "起始于")
    task_to_run = _find("Task To Run", "要运行的任务")
    start_time = _find("Start Time", "开始时间")
    task_state = _find("Scheduled Task State", "计划任务状态")
    trigger_time = None
    if start_time:
        m = re.search(r"(\d{1,2}):(\d{2})", start_time)
        if m:
            trigger_time = f"{int(m.group(1)):02d}:{m.group(2)}"
    if trigger_time is None and next_run:
        trigger_time = _trigger_time_from_next_run(next_run)
    return {
        "task_exists": True,
        "task_name": task_name,
        "status": status or "未知",
        "state": task_state,
        "enabled": (task_state or "").lower() not in {"disabled", "已禁用"},
        "next_run": next_run,
        "trigger_time": trigger_time,
        "start_in": start_in,
        "task_to_run": task_to_run,
    }


def _trigger_time_from_next_run(next_run: str | None) -> str | None:
    """从下次运行时间提取 HH:MM。"""
    if not next_run:
        return None
    m = re.search(r"(\d{1,2}):(\d{2})", next_run)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def _send_time_of(account_id: str | None) -> str | None:
    """从账号任务文件读取发送时间 HH:MM（保存计划时写入）。"""
    try:
        raw = _read_json(_account_task_path(account_id)) or {}
        st = str(raw.get("send_time") or "").strip()
        return st if re.fullmatch(r"\d{1,2}:\d{2}", st) else None
    except Exception:
        return None


def _account_enabled(account_id: str | None) -> bool:
    payload = _account_payload()
    return any(a["id"] == (account_id or "default") and a["enabled"] for a in payload["accounts"])


def _schedule_command(account_id: str | None, slot: str | None = None) -> str:
    if account_id and account_id != "default" and not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
        raise ConfigError("账号 id 含有不支持的字符")
    command = f'cmd /c "cd /d {PROJECT_ROOT} && {PYTHON} run.py'
    if account_id and account_id != "default":
        command += f" --account {account_id}"
    if slot:
        command += f" --time-slot {normalize_send_time(slot, '计划触发时间')}"
    return command + ' --source scheduled"'


def _slot_task_name(account_id: str | None, slot: str, default_time: str) -> str:
    return (_account_schedule_name(account_id) if slot == default_time else
            f"{_account_schedule_name(account_id)}-T{slot.replace(':', '')}")


def _schedule_manifest_path(account_id: str | None) -> Path:
    return _account_artifacts_dir(account_id) / "schedule_slots.json"


def _known_custom_slots(account_id: str | None) -> list[str]:
    manifest = _read_json(_schedule_manifest_path(account_id)) or {}
    return [slot for slot in manifest.get("custom_slots", [])
            if isinstance(slot, str) and re.fullmatch(r"\d{2}:\d{2}", slot)]


def _run_schtasks(args: list[str]) -> None:
    try:
        proc = subprocess.run(["schtasks", *args], capture_output=True, timeout=12)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"Windows 计划任务操作失败: {exc}") from exc
    if proc.returncode:
        raise ConfigError(_decode_command_output(proc.stderr or proc.stdout).strip() or "Windows 计划任务操作失败")


def _decode_command_output(value: bytes | None) -> str:
    if not value:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def _sync_schedule(account_id: str | None, send_time: str, *, custom: bool = False) -> None:
    name = (_slot_task_name(account_id, send_time, "") if custom else _account_schedule_name(account_id))
    _run_schtasks(["/create", "/tn", name, "/sc", "daily", "/st", send_time,
                   "/tr", _schedule_command(account_id, send_time if custom else None),
                   "/rl", "LIMITED", "/it", "/f"])
    battery_settings = (
        f"$t=Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop; "
        "$t.Settings.DisallowStartIfOnBatteries=$false; "
        "$t.Settings.StopIfGoingOnBatteries=$false; "
        "Set-ScheduledTask -InputObject $t -ErrorAction Stop | Out-Null; "
        f"$v=Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop; "
        "if($v.Settings.DisallowStartIfOnBatteries -or $v.Settings.StopIfGoingOnBatteries){throw '电池设置未生效'}"
    )
    try:
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", battery_settings],
                              capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"计划任务电池设置失败: {exc}") from exc
    if proc.returncode:
        raise ConfigError("计划任务已创建，但允许电池供电运行的设置未生效")
    actual = _schtasks_query(name)
    if not actual.get("task_exists") or not actual.get("enabled") or actual.get("trigger_time") != send_time:
        raise ConfigError("Windows 计划任务未通过读回校验")


def _disable_task(name: str) -> None:
    if _schtasks_query(name).get("task_exists"):
        _run_schtasks(["/change", "/tn", name, "/disable"])


def _sync_account_schedules(account_id: str | None) -> None:
    raw = _read_json(_account_task_path(account_id)) or {}
    default_time = normalize_send_time(raw.get("send_time"), "账号默认发送时间")
    slots = planned_slots(raw)
    old_custom_slots = _known_custom_slots(account_id)
    custom_slots = [slot for slot in slots if slot != default_time]
    # 先记录可能创建的任务，确保中途失败时仍能找到并停用它们。
    _write_json(_schedule_manifest_path(account_id),
                {"custom_slots": sorted(set(old_custom_slots + custom_slots))})
    if default_time in slots:
        _sync_schedule(account_id, default_time)
    else:
        _disable_task(_account_schedule_name(account_id))
    for slot in custom_slots:
        _sync_schedule(account_id, slot, custom=True)
    for slot in old_custom_slots:
        if slot not in custom_slots:
            _disable_task(_slot_task_name(account_id, slot, default_time))
    _write_json(_schedule_manifest_path(account_id), {"custom_slots": custom_slots})


def _disable_schedule(account_id: str | None) -> None:
    _disable_task(_account_schedule_name(account_id))
    for slot in _known_custom_slots(account_id):
        _disable_task(_slot_task_name(account_id, slot, ""))


def _effective_schedule(account_id: str | None = None) -> dict:
    """核对该账号每个发送时间对应的 Windows 任务。"""
    base_name = _account_schedule_name(account_id)
    st = _send_time_of(account_id)
    base = _schtasks_query(base_name)
    base["configured_time"] = st
    raw = _read_json(_account_task_path(account_id)) or {}
    base["schedule_mode"] = raw.get("schedule_mode", "legacy")
    if not _account_enabled(account_id):
        base.update(mode="disabled", next_run=None, trigger_time=st, status="账号已停用", slots=[])
        return base
    elif not st:
        base.update(mode="unsynced" if base.get("task_exists") and base.get("enabled") else "none",
                    next_run=None, status="未设置发送时间", slots=[],
                    error="账号没有设置发送时间，但系统中仍有启用的任务" if base.get("task_exists") and base.get("enabled") else None)
        return base
    try:
        slots = planned_slots(raw)
    except ConfigError as exc:
        base.update(mode="unsynced", next_run=None, status="好友发送时间无效", error=str(exc), slots=[])
        return base
    current = datetime.now().astimezone()
    details = []
    for slot in slots:
        name = _slot_task_name(account_id, slot, st)
        actual = base if slot == st else _schtasks_query(name)
        command = actual.get("task_to_run") or ""
        valid = (actual.get("task_exists") and actual.get("enabled") and actual.get("trigger_time") == slot
                 and str(PROJECT_ROOT) in command
                 and "--source scheduled" in command
                 and (not account_id or account_id == "default" or f"--account {account_id}" in command)
                 and ((f"--time-slot {slot}" in command) if slot != st else "--time-slot" not in command))
        due = current.replace(hour=int(slot[:2]), minute=int(slot[3:]), second=0, microsecond=0)
        if due <= current:
            due += timedelta(days=1)
        details.append({"time": slot, "task_name": name, "mode": "windows" if valid else "unsynced",
                        "next_run": due.strftime("%Y/%m/%d %H:%M:%S") if valid else None})
    stale_base = st not in slots and base.get("task_exists") and base.get("enabled")
    stale_custom = any(_schtasks_query(_slot_task_name(account_id, slot, "")).get("enabled")
                       for slot in _known_custom_slots(account_id) if slot not in slots)
    if not slots and not stale_base and not stale_custom:
        base.update(mode="none", status="没有启用的好友定时", next_run=None, slots=[])
        return base
    if details and all(item["mode"] == "windows" for item in details) and not stale_base and not stale_custom:
        first = min(details, key=lambda item: item["next_run"])
        base.update(mode="windows", status="就绪", next_run=first["next_run"],
                    trigger_time=first["time"], task_name=first["task_name"], slots=details)
    else:
        base.update(mode="unsynced", status="部分定时未生效", next_run=None, trigger_time=st,
                    error="部分好友时间对应的 Windows 任务缺失、停用或与配置不一致", slots=details)
    return base



def _overview_payload(account_id: str | None = None) -> dict:
    accounts = _account_payload()
    config = _config_payload(account_id)
    targets = config["targets"]
    schedule = _effective_schedule(account_id)
    arts = _account_artifacts_dir(account_id)
    result = _read_json(arts / "result.json")
    history = _read_json(arts / "history.json") or {}

    # 今日状态：看 history 是否有今天的 success 记录
    today = datetime.now().astimezone().date().isoformat()
    today_keys = [k for k in history if today in k and history[k].get("status") == "success"]
    today_status = "已发送" if today_keys else "未发送" if history else "无记录"
    last_time = None
    last_target = None
    for k, v in history.items():
        t = v.get("finished_at") or v.get("started_at")
        if t and (last_time is None or t > last_time):
            last_time = t
            parts = k.split(":", 3)
            last_target = parts[2] if len(parts) > 2 else None
    last_sent = None
    if last_time:
        try:
            dt = datetime.fromisoformat(last_time)
            last_sent = f"{dt.strftime('%H:%M')} 发往 {last_target or '好友'}"
        except Exception:
            pass
    return {
        "account_id": account_id or "default",
        "account_count": len(accounts["accounts"]),
        "enabled_account_count": sum(1 for a in accounts["accounts"] if a["enabled"]),
        "friend_count": len(targets),
        "next_run": schedule.get("next_run"),
        "trigger_time": schedule.get("trigger_time") or _trigger_time_from_next_run(schedule.get("next_run")),
        "today_status": today_status,
        "last_sent": last_sent,
        "last_run_result": result,
        "schedule": schedule,
    }


def _logs_payload(account_id: str | None = None, limit: int = 12) -> dict:
    arts = _account_artifacts_dir(account_id)
    history = _read_json(arts / "history.json") or {}
    journal = _read_json(arts / "run-journal.json") or []
    entries = []
    for key, value in history.items():
        parts = key.split(":", 3)
        entries.append({
            "task_id": parts[0] if len(parts) > 0 else "",
            "date": parts[1] if len(parts) > 1 else "",
            "target": parts[2] if len(parts) > 2 else "",
            "message_id": parts[3] if len(parts) > 3 else "",
            "status": value.get("status"),
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
        })
    entries.sort(key=lambda e: e.get("finished_at") or e.get("started_at") or "", reverse=True)
    runs = []
    if isinstance(journal, list):
        for item in journal:
            if not isinstance(item, dict):
                continue
            try:
                sent = int(item.get("sent", 0) or 0)
            except (TypeError, ValueError):
                sent = 0
            runs.append({
                "finished_at": item.get("finished_at") if isinstance(item.get("finished_at"), str) else "",
                "scope": item.get("scope") if item.get("scope") in {"global", "friend", "scheduled"} else "legacy",
                "target": item.get("target") if isinstance(item.get("target"), str) else "好友",
                "status": item.get("status") if isinstance(item.get("status"), str) else "unknown",
                "sent": sent,
            })
    runs.sort(key=lambda item: item["finished_at"], reverse=True)
    log_path = arts / "run.log"
    tail: list[str] = []
    if log_path.is_file():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-40:]
        except Exception:
            pass
    return {
        "account_id": account_id or "default",
        "entries": entries[:limit],
        "runs": runs[:limit],
        "result": _read_json(arts / "result.json"),
        "metrics": _read_json(arts / "metrics.json"),
        "schedule_status": read_status(arts / "scheduled.json"),
        "catchup_status": read_status(arts / "catchup.json"),
        "log_tail": tail,
        "total_entries": len(entries),
    }


def _run_lock_exists(account_id: str | None = None) -> bool:
    return (_account_artifacts_dir(account_id) / "run.lock").exists()


def _validate_run_account(account_id: str | None) -> str | None:
    accounts = _account_payload()
    if accounts["mode"] == "multi" and not account_id:
        raise ConfigError("请选择要运行的账号")
    account_id = account_id or "default"
    account = next((a for a in accounts["accounts"] if a["id"] == account_id), None)
    if account is None:
        raise ConfigError("所选账号不存在")
    if not account["enabled"]:
        raise ConfigError("所选账号已停用")
    return account_id


def _spawn_run(dry_run: bool, account_id: str | None = None,
               target_name: str | None = None, revision: str | None = None,
               prevent_duplicates: bool | None = None) -> dict:
    """后台子进程执行 run.py（--dry-run 为试运行）。account_id 指定则只运行该账号。"""
    global _run_seq
    with _lock:
        if any(t.get("status") in ("starting", "running") and
               t.get("account_id") == (account_id or "default") for t in _run_tasks.values()):
            raise ConfigError("当前账号已有任务正在启动或运行，请等待完成")
        _run_seq += 1
        seq = _run_seq
        _run_tasks[seq] = {"mode": "dry" if dry_run else "now", "status": "starting", "proc": None, "account_id": account_id or "default", "started_at": datetime.now().astimezone().isoformat()}
    args = [str(PYTHON), "run.py"]
    if dry_run:
        args.append("--dry-run")
    if account_id and account_id != "default":
        args += ["--account", account_id]
    if target_name is not None:
        args += ["--target", target_name, "--task-revision", revision or ""]
        if prevent_duplicates is not None:
            args.append("--prevent-duplicates" if prevent_duplicates else "--allow-duplicates")
    try:
        proc = subprocess.Popen(
            args, cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        with _lock:
            _run_tasks[seq]["status"] = "failed"
        raise
    with _lock:
        _run_tasks[seq]["proc"] = proc
        _run_tasks[seq]["status"] = "running"
        _run_tasks[seq]["pid"] = proc.pid

    def _watch(seq_: int, proc_: subprocess.Popen):
        proc_.wait()
        with _lock:
            task = _run_tasks[seq_]
            task["status"] = "success" if proc_.returncode == 0 else "failed"
            task["returncode"] = proc_.returncode
            task["finished_at"] = datetime.now().astimezone().isoformat()
            if proc_.returncode != 0:
                result = _read_json(_account_artifacts_dir(account_id) / "result.json") or {}
                if result.get("finished_at", "") >= task["started_at"]:
                    task["error"] = next((r.get("error") for r in result.get("results", []) if r.get("error")), None)

    threading.Thread(target=_watch, args=(seq, proc), daemon=True).start()
    return {"id": seq, "mode": "dry" if dry_run else "now", "status": "running"}


def _spawn_relogin(account_id: str | None = None) -> dict:
    """后台子进程执行 scripts/login_auto.py（弹出浏览器扫码）。

    account_id 为空：单账号模式；否则多账号登录该账号（独立凭证文件）。
    """
    global _run_seq
    with _lock:
        # 防重：已有进行中的重登录任务则拒绝新任务（避免弹出多个登录窗口）
        for t in _run_tasks.values():
            if t.get("mode") == "relogin" and t.get("status") in ("starting", "running"):
                raise ConfigError("已有重登录流程在运行，请在弹出的浏览器窗口完成扫码后再试")
        _run_seq += 1
        seq = _run_seq
        _run_tasks[seq] = {"mode": "relogin", "status": "starting", "proc": None, "started_at": datetime.now().astimezone().isoformat()}
    args = [str(PYTHON), "scripts/login_auto.py"]
    if account_id:
        args += ["--account", account_id]
    proc = subprocess.Popen(
        args, cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    with _lock:
        _run_tasks[seq]["proc"] = proc
        _run_tasks[seq]["status"] = "running"
        _run_tasks[seq]["pid"] = proc.pid

    def _watch(seq_: int, proc_: subprocess.Popen):
        proc_.wait()
        with _lock:
            task = _run_tasks[seq_]
            task["status"] = "success" if proc_.returncode == 0 else "failed"
            task["returncode"] = proc_.returncode
            task["finished_at"] = datetime.now().astimezone().isoformat()

    threading.Thread(target=_watch, args=(seq, proc), daemon=True).start()
    return {"id": seq, "mode": "relogin", "status": "running"}


def _task_status(seq: int) -> dict | None:
    with _lock:
        task = _run_tasks.get(seq)
        if not task:
            return None
        return {k: v for k, v in task.items() if k != "proc"}


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinAutoFireWeb/0.1"

    def log_message(self, fmt, *args):  # 静默访问日志
        return

    def _send(self, status: int, body: Any, content_type: str = "application/json; charset=utf-8") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data: Any, status: int = 200) -> None:
        self._send(status, data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _route_api(self, method: str, path: str, query: dict) -> None:
        parts = [p for p in path.split("/") if p]
        # /api/xxx
        if not parts or parts[0] != "api":
            self._send_json({"error": "not found"}, 404)
            return
        seg = parts[1:] if len(parts) > 1 else []

        def _fail(exc: Exception) -> None:
            self._send_json({"error": str(exc), "type": type(exc).__name__}, 500)

        def _account_from_query(query: dict) -> str | None:
            """从 query 取 account 参数；缺省返回 None（default 账号）。"""
            value = (query.get("account", [""])[0] or "").strip()
            if value and value not in {item["id"] for item in _account_payload()["accounts"]}:
                raise ConfigError("账号不存在，不能读取或修改其配置")
            return value or None

        try:
            if seg == [] and method == "GET":
                self._send_json({"name": "douyin-auto-fire-web", "version": "0.1"})
            elif seg == ["bootstrap"] and method == "GET":
                account_id = _account_from_query(query)
                self._send_json({
                    "account_id": account_id or "default",
                    "config": _config_payload(account_id),
                    "accounts": _account_payload(),
                    "notify": _notify_payload(),
                    "overview": _overview_payload(account_id),
                    "version": f"v{APP_VERSION}",
                    "validation": validation_status(account_id or "default", _account_task_path(account_id),
                                                    _account_artifacts_dir(account_id)),
                    "logs": _logs_payload(account_id),
                    "run_lock": _run_lock_exists(account_id),
                })
            elif seg == ["config"] and method == "GET":
                self._send_json(_config_payload(_account_from_query(query)))
            elif seg == ["config"] and method == "PUT":
                self._save_config(self._read_body(), _account_from_query(query))
            elif seg == ["accounts"] and method == "GET":
                self._send_json(_account_payload())
            elif seg == ["accounts"] and method == "PUT":
                self._save_accounts(self._read_body())
            elif seg == ["accounts"] and method == "POST":
                body = self._read_body()
                self._send_json({"ok": True, "accounts": _add_account(str(body.get("label", "")).strip())}, 201)
            elif seg == ["accounts"] and method == "DELETE":
                account_id = (query.get("id", [""])[0] or "").strip()
                if not account_id:
                    raise ConfigError("缺少账号 id 参数")
                self._send_json({"ok": True, "accounts": _remove_account(account_id)})
            elif seg == ["accounts", "relogin"] and method == "POST":
                body = self._read_body()
                account_id = str(body.get("id", "")).strip() or None
                info = _spawn_relogin(account_id)
                self._send_json(info, 202)
            elif seg == ["notify"] and method == "GET":
                self._send_json(_notify_payload())
            elif seg == ["notify"] and method == "PUT":
                self._save_notify(self._read_body())
            elif seg == ["schedule"] and method == "GET":
                self._send_json(_effective_schedule(_account_from_query(query)))
            elif seg == ["schedule"] and method == "PUT":
                self._save_schedule(self._read_body(), _account_from_query(query))
            elif seg == ["run", "friend"] and method == "POST":
                body = self._read_body()
                account_id = _validate_run_account(str(body.get("account", "")).strip() or None)
                name, revision = body.get("target"), body.get("task_revision")
                prevent_duplicates = body.get("prevent_duplicates")
                if not isinstance(name, str) or not name.strip() or not isinstance(revision, str):
                    raise ConfigError("单好友发送缺少好友或配置版本，请刷新后重试")
                if prevent_duplicates is not None and not isinstance(prevent_duplicates, bool):
                    raise ConfigError("防重复设置必须是开关状态")
                path = _account_task_path(account_id)
                require_revision(path, revision)
                targets = _parse_task_raw(_read_json(path) or {})["targets"]
                if len([target for target in targets if target["name"] == name]) != 1:
                    raise ConfigError("当前账号下未找到唯一的指定好友，已停止发送")
                require_revision(path, revision)
                if _run_lock_exists(account_id):
                    self._send_json({"error": "当前账号已有任务正在运行，请稍后再试"}, 409)
                    return
                require_validation(account_id or "default", path, _account_artifacts_dir(account_id))
                self._send_json(_spawn_run(False, account_id, name, revision, prevent_duplicates), 202)
            elif seg == ["run"] and method == "POST":
                body = self._read_body()
                account_id = _validate_run_account(str(body.get("account", "")).strip() or None)
                if _run_lock_exists(account_id):
                    self._send_json({"error": "已有任务正在运行（run.lock 存在），请稍后再试"}, 409)
                    return
                dry = body.get("dry_run", False)
                if not dry:
                    require_validation(account_id or "default", _account_task_path(account_id),
                                       _account_artifacts_dir(account_id))
                self._send_json(_spawn_run(bool(dry), account_id), 202)
            elif seg == ["run", "status"] and method == "GET":
                seq = int(query.get("id", ["0"])[0] or 0)
                status = _task_status(seq)
                if status is None:
                    self._send_json({"error": "任务不存在"}, 404)
                    return
                self._send_json(status)
            elif seg == ["logs"] and method == "GET":
                self._send_json(_logs_payload(_account_from_query(query)))
            elif seg == ["stickers"] and method == "GET":
                catalog_path = PROJECT_ROOT / "config" / "stickers_catalog.json"
                if catalog_path.is_file():
                    self._send_json(_read_json(catalog_path))
                else:
                    self._send_json({"categories": [], "note": "未生成表情目录，请先运行 scripts/scrape_stickers.py"})
            elif seg == ["env"] and method == "GET":
                env = _read_env()
                self._send_json({
                    "headless": env.get("HEADLESS", "false") in ("1", "true", "yes", "on"),
                    "trace": env.get("TRACE", "true") in ("1", "true", "yes", "on"),
                    "artifacts_dir": env.get("ARTIFACTS_DIR", "artifacts"),
                    "task_config": env.get("TASK_CONFIG", "config.json"),
                })
            else:
                self._send_json({"error": "not found", "method": method, "path": path}, 404)
        except ConfigError as exc:
            self._send_json({"error": str(exc), "type": "ConfigError"}, 400)
        except Exception as exc:
            _fail(exc)

    # ---- 写操作实现 ----

    def _normalize_message(self, msg: dict) -> dict:
        """把前端统一格式 {type, content} 转回 config.json 原格式（value/path/sticker/choices）。"""
        mtype = msg.get("type", "text")
        if mtype == "text":
            return {"type": "text", "value": msg.get("content", "")}
        if mtype == "image":
            return {"type": "image", "path": msg.get("path", msg.get("content", ""))}
        if mtype in ("douyin_sticker", "sticker"):
            return {"type": "douyin_sticker", "sticker": msg.get("sticker", msg.get("content", ""))}
        if mtype == "random":
            return {"type": "random", "choices": [self._normalize_message(c) for c in msg.get("choices", [])]}
        return dict(msg)

    def _save_config(self, body: dict, account_id: str | None = None) -> None:
        """保存好友与消息配置到该账号的任务文件。
        body: {global_messages, targets:[{name,messages,content_mode}], send_interval, prevent_duplicates, stickers}
        """
        if not account_id and _account_payload()["mode"] == "multi":
            raise ConfigError("请先选择账号，再保存好友和消息")
        task_path = _account_task_path(account_id)
        raw = _read_json(task_path) or {}
        targets = body.get("targets")
        if not isinstance(targets, list):
            raise ConfigError("targets 必须是数组")
        global_messages = body.get("global_messages")
        if not isinstance(global_messages, list):
            global_messages = _parse_task_raw(raw)["global_messages"]
        normalized_global = [self._normalize_message(message) for message in global_messages]
        existing_ids = {str(item.get("name", "")).strip(): item.get("douyin_id")
                        for item in raw.get("targets", []) if isinstance(item, dict)}
        parsed_targets = []
        for item in targets:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise ConfigError("target 缺少 name")
            content_mode = item.get("content_mode", "custom")
            if content_mode not in ("global", "custom"):
                raise ConfigError(f"好友「{item['name']}」的内容模式无效")
            messages = global_messages if content_mode == "global" else item.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ConfigError(f"好友「{item['name']}」缺少消息列表")
            parsed_targets.append({
                "name": str(item["name"]).strip(),
                "douyin_id": normalize_douyin_id(item.get("douyin_id", existing_ids.get(str(item["name"]).strip()))),
                "messages": [self._normalize_message(m) for m in messages],
                "content_mode": content_mode,
                "send_time": normalize_send_time(item["send_time"], f"好友「{item['name']}」的发送时间")
                if item.get("send_time") else None,
                "schedule_enabled": item.get("schedule_enabled", True),
            })
            if not isinstance(parsed_targets[-1]["schedule_enabled"], bool):
                raise ConfigError(f"好友「{item['name']}」的定时开关无效")
        schedule_mode = body.get("schedule_mode", raw.get("schedule_mode", "legacy"))
        if schedule_mode not in ("legacy", "global", "friends"):
            raise ConfigError("定时模式无效")
        if schedule_mode == "friends":
            default_time = raw.get("send_time")
            for target in parsed_targets:
                if target["schedule_enabled"] and not target["send_time"]:
                    target["send_time"] = normalize_send_time(default_time, "账号原发送时间")
        new_raw: dict[str, Any] = {}
        preserved = {
            k: v for k, v in raw.items()
            if k not in ("friends", "messages", "targets", "global_messages", "send_interval_seconds", "prevent_duplicates")
        }
        new_raw["global_messages"] = normalized_global
        new_raw["targets"] = parsed_targets
        new_raw["send_interval_seconds"] = {
            "min": float(body.get("send_interval", {}).get("min", 3)),
            "max": float(body.get("send_interval", {}).get("max", 8)),
        }
        new_raw["prevent_duplicates"] = bool(body.get("prevent_duplicates", raw.get("prevent_duplicates", False)))
        new_raw.update(preserved)
        new_raw["schedule_mode"] = schedule_mode
        # stickers：前端传入优先；否则合并全局表情映射（config/stickers.json），
        # 保证该账号任务文件独立可发送原生表情。
        if isinstance(body.get("stickers"), dict) and body["stickers"]:
            new_raw["stickers"] = body["stickers"]
        else:
            global_stickers = _read_json(PROJECT_ROOT / "config" / "stickers.json") or {}
            if global_stickers:
                new_raw["stickers"] = global_stickers
        task_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=task_path.parent,
                                         prefix=f".{task_path.stem}-", suffix=".json", delete=False) as stream:
            json.dump(new_raw, stream, ensure_ascii=False, indent=2)
            candidate = Path(stream.name)
        try:
            # 用真实解析器先校验候选文件，验证通过后才原子替换正式配置。
            if parsed_targets:
                settings = load_settings()
                settings = settings.__class__(
                    task_config_path=candidate,
                    storage_state=settings.storage_state,
                    cookie=settings.cookie,
                    headless=settings.headless,
                    browser_path=settings.browser_path,
                    artifacts_dir=settings.artifacts_dir,
                    trace=settings.trace,
                    dingtalk_webhook=settings.dingtalk_webhook,
                    dingtalk_secret=settings.dingtalk_secret,
                    webhook_url=settings.webhook_url,
                    webhook_headers=settings.webhook_headers,
                    webhook_template=settings.webhook_template,
                )
                load_task(settings)
            os.replace(candidate, task_path)
        finally:
            candidate.unlink(missing_ok=True)
        warn = None
        enabled = _account_enabled(account_id)
        changed_schedule = (planned_slots(raw) != planned_slots(new_raw)
                            or raw.get("schedule_mode", "legacy") != schedule_mode)
        if enabled and (changed_schedule or _effective_schedule(account_id)["mode"] == "unsynced"):
            try:
                _sync_account_schedules(account_id)
            except ConfigError as exc:
                warn = f"好友设置已保存，但系统定时未同步：{exc}"
        schedule = _effective_schedule(account_id) if enabled else None
        if schedule and schedule["mode"] not in ("windows", "none"):
            warn = warn or "好友设置已保存，但系统定时未通过读回校验"
        reply = {"ok": True, "format": "targets", "config": _config_payload(account_id)}
        if warn:
            reply["warn"] = warn
        if schedule:
            reply["schedule"] = schedule
        self._send_json(reply)

    def _save_accounts(self, body: dict) -> None:
        accounts = body.get("accounts")
        if not isinstance(accounts, list):
            raise ConfigError("accounts 必须是非空数组")
        accounts_file = PROJECT_ROOT / "config" / "accounts.json"
        if not accounts_file.is_file():
            raise ConfigError("当前为单账号模式，无法保存账号列表")
        raw = _read_json(accounts_file) or {"accounts": []}
        # 保留原有字段，仅更新 id/enabled/label；顺序不变
        by_id = {a.get("id"): a for a in raw.get("accounts", [])}
        new_list = []
        for item in accounts:
            account_id = str(item.get("id", ""))
            if not account_id:
                continue
            if account_id not in by_id:
                raise ConfigError("账号不存在；请使用添加账号功能")
            base = dict(by_id.get(account_id) or {})
            base["id"] = account_id
            base["enabled"] = bool(item.get("enabled", base.get("enabled", True)))
            if item.get("label"):
                base["label"] = str(item["label"])
            new_list.append(base)
        for item in new_list:
            old = by_id.get(item["id"], {})
            if item["enabled"] == bool(old.get("enabled", True)):
                continue
            if item["enabled"]:
                send_time = _send_time_of(item["id"])
                if not send_time:
                    raise ConfigError(f"请先为账号 {item['id']} 设置每天发送时间")
                _sync_account_schedules(item["id"])
            else:
                _disable_schedule(item["id"])
        out = {"accounts": new_list}
        if body.get("default_id"):
            out["default_id"] = str(body["default_id"])
        _write_json(accounts_file, out)
        self._send_json({"ok": True, "accounts": _account_payload()})

    def _save_notify(self, body: dict) -> None:
        updates: dict[str, str | None] = {}
        for key in ("DINGTALK_WEBHOOK", "DINGTALK_SECRET", "WEBHOOK_URL", "WEBHOOK_HEADERS", "WEBHOOK_TEMPLATE"):
            val = body.get(key.lower())
            updates[key] = None if val in (None, "") else str(val)
        # 钉钉必须成对配置
        dw, ds = updates.get("DINGTALK_WEBHOOK"), updates.get("DINGTALK_SECRET")
        if bool(dw) != bool(ds):
            raise ConfigError("钉钉 Webhook 与 Secret 必须同时填写或同时清空")
        _update_env_file(updates)
        self._send_json({"ok": True, "notify": _notify_payload()})

    def _save_schedule(self, body: dict, account_id: str | None = None) -> None:
        if not account_id and _account_payload()["mode"] == "multi":
            raise ConfigError("请先选择账号，再保存定时")
        value = str(body.get("time", "")).strip()
        st = normalize_send_time(value, "每天发送时间")
        tp = _account_task_path(account_id)
        raw = _read_json(tp) or {}
        mode = body.get("mode", raw.get("schedule_mode", "global"))
        if mode not in ("global", "friends"):
            raise ConfigError("定时模式无效")
        raw["send_time"] = st
        raw["schedule_mode"] = mode
        if mode == "friends":
            for target in raw.get("targets", []):
                if target.get("schedule_enabled", True) and not target.get("send_time"):
                    target["send_time"] = st
        _write_json(tp, raw)
        warn = None
        try:
            if _account_enabled(account_id):
                _sync_account_schedules(account_id)
            else:
                _disable_schedule(account_id)
        except ConfigError as exc:
            warn = f"发送时间已保存，但系统定时未生效：{exc}"
        schedule = _effective_schedule(account_id)
        if _account_enabled(account_id) and schedule["mode"] not in ("windows", "none"):
            warn = warn or "发送时间已保存，但系统定时未通过读回校验"
        self._send_json({"ok": True, "account_id": account_id or "default",
                         "warn": warn, "schedule": schedule})

    # ---- 静态页面 ----

    def _serve_static(self, path: str) -> bool:
        """托管 assets/ 下的静态资源（表情缩略图等）。只允许 assets 目录内文件。"""
        if not path.startswith("/assets/"):
            return False
        rel = unquote(path[len("/assets/"):])
        candidate = (PROJECT_ROOT / "assets" / rel).resolve()
        assets_root = (PROJECT_ROOT / "assets").resolve()
        try:
            candidate.relative_to(assets_root)
        except ValueError:
            self._send_json({"error": "forbidden"}, 403)
            return True
        if not candidate.is_file():
            self._send_json({"error": "not found"}, 404)
            return True
        ext = candidate.suffix.lower()
        mime = {
            ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml",
            ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        }.get(ext, "application/octet-stream")
        data = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if self._serve_static(path):
            return
        if path == "/" or path == "/index.html":
            index = PROJECT_ROOT / "web" / "index.html"
            if not index.is_file():
                self._send(500, {"error": "web/index.html 不存在"})
                return
            data = index.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self._route_api("GET", path, query)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        self._route_api("POST", parsed.path, parse_qs(parsed.query))

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        self._route_api("PUT", parsed.path, parse_qs(parsed.query))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        self._route_api("DELETE", parsed.path, parse_qs(parsed.query))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"自动续火花 · 本地控制台已启动: {url}")
    print(f"当前模式: 单账号" if (PROJECT_ROOT / "config" / "accounts.json").is_file() is False else "当前模式: 多账号")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

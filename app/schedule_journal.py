"""按账号保存正常定时与错时补跑的本地状态。"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


def read_status(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and value.get("status") else {"status": "corrupt"}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"status": "corrupt"}


def write_status(path: Path, status: str, *, now: datetime | None = None, detail: str = "") -> dict:
    now = now or datetime.now().astimezone()
    value = {"date": now.date().isoformat(), "at": now.isoformat(),
             "status": status, "detail": detail}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".schedule-", suffix=".json", delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        candidate = Path(stream.name)
    try:
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)
    return value


def delivery_evidence(history_path: Path, day: str) -> str:
    """返回 sent、unknown 或 none；损坏的历史按结果不明处理。"""
    send_marker = read_status(history_path.parent / "send-day.json")
    if send_marker.get("status") == "corrupt":
        return "unknown"
    evidence = "none"
    if send_marker.get("date") == day:
        if send_marker.get("status") == "unknown_send":
            evidence = "unknown"
        elif send_marker.get("status") == "sent":
            evidence = "sent"
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        history = {}
    except (OSError, ValueError):
        return "unknown"
    if not isinstance(history, dict):
        return "unknown"
    if any(not isinstance(item, dict) for item in history.values()):
        return "unknown"
    for key, item in history.items():
        if f":{day}:" not in key:
            continue
        if item.get("status") == "unknown":
            return "unknown"
        if item.get("status") == "success":
            evidence = "sent"
        elif item.get("status") != "failed":
            return "unknown"
    if evidence == "unknown":
        return evidence
    try:
        result = json.loads((history_path.parent / "result.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return evidence
    except (OSError, ValueError):
        return "unknown"
    if not isinstance(result, dict):
        return "unknown"
    if str(result.get("finished_at", "")).startswith(day) and result.get("dry_run") is False:
        try:
            if any(int(item.get("sent") or 0) > 0 for item in result.get("results", [])):
                evidence = "sent"
        except (AttributeError, TypeError, ValueError):
            return "unknown"
    return evidence


def sent_today(history_path: Path, day: str) -> bool:
    """兼容旧调用：成功或结果不明都禁止自动重发。"""
    return delivery_evidence(history_path, day) != "none"

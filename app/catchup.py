"""电脑恢复可运行后检查当天漏触发的账号；实际启用依赖另行配置 Windows 任务。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, time
from pathlib import Path

from app.accounts import load_accounts
from app.account_runner import account_env
from app.config import load_settings
from app.history import AlreadyRunningError, run_lock
from app.schedule_journal import delivery_evidence, read_status, write_status
from app.schedule_slots import planned_slots
from app.validation import validation_status


ROOT = Path(__file__).resolve().parents[1]
GRACE = timedelta(minutes=10)


def due_reason(now: datetime, send_time: str, scheduled: dict, catchup: dict,
               history_path: Path) -> str:
    """返回 due 或不应补跑的原因；不读取登录状态、不触发发送。"""
    hour, minute = map(int, send_time.split(":"))
    due_at = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo) + GRACE
    if now < due_at:
        return "not_due"
    if scheduled.get("status") == "corrupt" or catchup.get("status") == "corrupt":
        return "journal_failed"
    day = now.date().isoformat()
    if catchup.get("date") == day and catchup.get("status") not in ("validation_failed", "config_failed"):
        return "already_checked"
    evidence = delivery_evidence(history_path, day)
    if evidence == "unknown":
        return "result_unknown"
    if evidence == "sent":
        return "already_sent"
    if scheduled.get("date") == day:
        if scheduled.get("status") == "normal_triggered":
            return "normal_pending"
        if scheduled.get("status") not in ("normal_failed", "login_failed", "normal_finished"):
            return "result_unknown"
    return "due"


def _classify_result(artifacts: Path, started_at: str, returncode: int,
                     output: str, day: str) -> tuple[str, str]:
    try:
        result = json.loads((artifacts / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {}
    fresh = result.get("finished_at", "") >= started_at and result.get("dry_run") is False
    evidence = delivery_evidence(artifacts / "history.json", day)
    if evidence == "unknown":
        return "result_unknown", "发送结果不明，停止自动重发；请核对客户端"
    if evidence == "sent":
        sent = sum(int(item.get("sent") or 0) for item in result.get("results", [])) if fresh else 0
        return "sent", f"程序记录发送 {sent} 条；客户端到达仍待验收"
    if returncode == 0 and fresh:
        return "no_send", "运行完成但程序未记录发送；当天不再自动尝试"
    errors = " ".join(str(item.get("error") or "") for item in result.get("results", [])) if fresh else ""
    if "CATCHUP_EXPIRED" in output or "CATCHUP_EXPIRED" in errors:
        return "expired", "已跨天，补跑停止，未自动积压到次日"
    if "AuthenticationError" in output or "登录" in output or "登录" in errors:
        return "login_failed", "登录状态失效或登录检查失败"
    return "process_failed", f"运行失败，退出码 {returncode}"


def check_once(now: datetime | None = None, root: Path = ROOT,
               invoke=subprocess.run) -> dict[str, str]:
    """每次检查所有启用账号；只在 due 时运行正式命令。"""
    now = now or datetime.now().astimezone()
    outcome: dict[str, str] = {}
    try:
        with run_lock(root / "artifacts" / "catchup-check.lock"):
            for account in load_accounts(root / "config" / "accounts.json") or []:
                artifacts = root / "artifacts" / account.id
                journal = artifacts / "catchup.json"
                try:
                    with account_env(root / account.env_file,
                                     defaults={"ARTIFACTS_DIR": str(artifacts)}):
                        settings = load_settings(None)
                    task_path = root / settings.task_config_path
                    result_dir = root / settings.artifacts_dir
                    task = json.loads(task_path.read_text(encoding="utf-8"))
                    send_time = str(task["send_time"])
                    if task.get("schedule_mode") == "friends" or planned_slots(task) != [send_time]:
                        write_status(journal, "config_failed", now=now,
                                     detail="好友有独立发送时间，错时补跑暂不可用；未自动发送")
                        outcome[account.id] = "config_failed"
                        continue
                    reason = due_reason(now, send_time,
                                        read_status(artifacts / "scheduled.json"),
                                        read_status(journal), result_dir / "history.json")
                    if reason != "due":
                        outcome[account.id] = reason
                        if reason in ("already_sent", "result_unknown", "journal_failed"):
                            detail = {"already_sent": "当天已有程序发送记录；客户端到达仍待验收",
                                      "result_unknown": "发送结果不明，停止自动重发；请核对客户端",
                                      "journal_failed": "本地定时状态记录异常，已停止补跑"}[reason]
                            write_status(journal, reason, now=now, detail=detail)
                        continue
                    if validation_status(account.id, task_path,
                                         result_dir, root)["status"] != "passed":
                        write_status(journal, "validation_failed", now=now,
                                     detail="当前配置或代码未通过无发送试运行")
                        outcome[account.id] = "validation_failed"
                        continue
                    write_status(journal, "starting", now=now, detail="错时补跑已启动")
                    try:
                        proc = invoke([sys.executable, "run.py", "--account", account.id],
                                      cwd=root, capture_output=True, text=True, errors="replace",
                                      env={**os.environ, "DOUYIN_CATCHUP_DATE": now.date().isoformat()})
                    except OSError:
                        write_status(journal, "process_start_failed", now=now,
                                     detail="补跑进程未能启动")
                        outcome[account.id] = "process_start_failed"
                        continue
                    status, detail = _classify_result(result_dir, now.isoformat(),
                                                       proc.returncode, (proc.stdout or "") + (proc.stderr or ""),
                                                       now.date().isoformat())
                    write_status(journal, status, now=now, detail=detail)
                    outcome[account.id] = status
                except (OSError, ValueError, KeyError) as exc:
                    write_status(journal, "config_failed", now=now,
                                 detail=f"补跑配置检查失败：{type(exc).__name__}")
                    outcome[account.id] = "config_failed"
    except AlreadyRunningError:
        return {"checker": "already_running"}
    return outcome

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import dotenv_values, load_dotenv

from app.accounts import load_accounts
from app.config import ConfigError, load_settings
from app.history import run_lock
from app.main import LOGGER, _configure_logging, _parse_cli_args, run
from app.schedule_journal import write_status
from app.validation import record_validation, require_validation


# 单账号模式的旧环境变量。多账号模式下由各账号的 env 文件提供，
# 启动时先清掉进程环境中的旧值，避免残留值被所有账号继承。
_LEGACY_ENV_KEYS = ("DOUYIN_COOKIE", "DOUYIN_STORAGE_STATE", "TASK_CONFIG", "ARTIFACTS_DIR")


def run_all_accounts(only: str | None = None) -> int:
    """串行执行 accounts.json 中启用账号（only 指定时只跑该账号）。

    单账号失败（Cookie 失效、好友不存在、发送异常等）只记为该账号
    failed，不阻止其他账号运行。返回码与单账号语义一致：
    0=全部成功，1=存在失败，2=多账号配置整体错误。
    """
    args = _parse_cli_args()
    target_name = getattr(args, "target", None)
    if ((target_name is not None and (not only or getattr(args, "source", "manual") != "manual"))
            or (getattr(args, "prevent_duplicates", None) is not None and target_name is None)):
        print("错误: 单好友发送必须指定账号，并使用手动运行")
        return 2
    if only:
        only = only.strip()
    accounts = load_accounts()
    if not accounts:
        if only:
            print(f"错误: 账号 {only} 不存在或已停用，禁止发送")
            return 2
        print("没有启用任何账号，本次任务跳过")
        return 0
    if only:
        matched = [a for a in accounts if a.id == only]
        if not matched:
            print(f"错误: 账号 {only} 不存在或已停用，禁止发送")
            return 2
        accounts = matched
    if args.env_file:
        load_dotenv(args.env_file)
    for key in _LEGACY_ENV_KEYS:
        os.environ.pop(key, None)

    _configure_logging(Path("artifacts"), label=None, reset=True)
    LOGGER.info("多账号模式：共 %d 个启用账号", len(accounts))

    summary: list[tuple[str, str, str | None]] = []
    for account in accounts:
        scheduled_path = Path("artifacts") / account.id / "scheduled.json"
        if getattr(args, "source", "manual") == "scheduled" and not args.dry_run:
            write_status(scheduled_path, "normal_triggered", detail="Windows 每日任务已启动")
        # 先按默认产物目录配置账号日志，保证账号内任何失败都带 [账号id] 前缀；
        # 若账号 env 显式指定了 ARTIFACTS_DIR，进入账号环境后会重定向。
        _configure_logging(Path("artifacts") / account.id, label=account.id, reset=True)
        LOGGER.info("开始执行任务")
        try:
            with account_env(account.env_file, defaults={"ARTIFACTS_DIR": f"artifacts/{account.id}"}):
                settings = load_settings(None)
                _configure_logging(settings.artifacts_dir, label=account.id, reset=True)
                with run_lock(settings.artifacts_dir / "run.lock"):
                    if not args.dry_run:
                        require_validation(account.id, settings.task_config_path, settings.artifacts_dir)
                    run_kwargs = {"dry_run": args.dry_run}
                    if target_name is not None:
                        run_kwargs.update(target_name=target_name, task_revision=getattr(args, "task_revision", None))
                    if getattr(args, "prevent_duplicates", None) is not None:
                        run_kwargs.update(prevent_duplicates=args.prevent_duplicates)
                    if getattr(args, "source", "manual") == "scheduled":
                        run_kwargs.update(source="scheduled", schedule_slot=getattr(args, "time_slot", None))
                    code = asyncio.run(run(**run_kwargs))
                if code == 0 and args.dry_run and target_name is None:
                    record_validation(account.id, settings.task_config_path, settings.artifacts_dir)
                    LOGGER.info("无发送试运行验证已记录: %s", account.id)
            status = "success" if code == 0 else "failed"
            if getattr(args, "source", "manual") == "scheduled" and not args.dry_run:
                write_status(scheduled_path, "normal_finished" if code == 0 else "normal_failed",
                             detail=f"程序退出码 {code}")
            summary.append((account.id, status, None))
            LOGGER.info("执行完成: %s", status)
        except Exception as exc:
            if getattr(args, "source", "manual") == "scheduled" and not args.dry_run:
                write_status(scheduled_path,
                             "login_failed" if type(exc).__name__ == "AuthenticationError" else "normal_failed",
                             detail=f"程序异常：{type(exc).__name__}")
            # 异常消息可能包含好友真名（如 Playwright 定位器超时），此处只记录
            # 异常类型；完整脱敏详情已由 run() 写入该账号的 run.log。
            summary.append((account.id, "failed", type(exc).__name__))
            LOGGER.exception("执行失败: %s", exc)

    _configure_logging(Path("artifacts"), label=None, reset=True)
    for account_id, status, error in summary:
        detail = f" - {error}" if error else ""
        LOGGER.info("[%s] 结果: %s%s", account_id, status, detail)
    failed = sum(1 for _, status, _ in summary if status == "failed")
    LOGGER.info("多账号执行结束: 成功 %d，失败 %d", len(summary) - failed, failed)
    return 1 if failed else 0


def _load_account_env(env_file: Path, defaults: dict[str, str] | None = None) -> dict[str, str]:
    env_file = Path(env_file)
    if not env_file.is_file():
        raise ConfigError(f"账号环境文件不存在: {env_file}")
    values = {key: value for key, value in (dotenv_values(env_file) or {}).items() if value is not None}
    for key, value in (defaults or {}).items():
        values.setdefault(key, value)
    return values


@contextmanager
def account_env(env_file: Path, defaults: dict[str, str] | None = None) -> Iterator[None]:
    """临时把账号 env 应用到进程环境，退出时完全恢复。

    - 账号 env 中的键覆盖进程环境已有值，退出时恢复原值；
    - 账号 env 新增的键，退出时删除，绝不泄漏给下一个账号；
    - 账号 env 未定义的键（如 CI 的 HEADLESS）保持继承进程环境。
    """
    values = _load_account_env(env_file, defaults)
    saved = {key: os.environ[key] for key in values if key in os.environ}
    fresh = set(values) - set(saved)
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.update(saved)
        for key in fresh:
            os.environ.pop(key, None)

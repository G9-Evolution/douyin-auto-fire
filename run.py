from app.accounts import load_accounts
from app.account_runner import run_all_accounts
from app.config import ConfigError
from app.main import _parse_cli_args, main as run_single


def main() -> int:
    # 入口和实际运行共用参数定义，避免工作台新增参数后入口漏接。
    args = _parse_cli_args()
    if (args.target is not None and args.source != "manual") or (args.task_revision is not None and args.target is None):
        print("错误: 单好友发送仅用于手动运行，配置版本必须与指定好友同时提供")
        return 2
    if args.prevent_duplicates is not None and args.target is None:
        print("错误: 防重复开关必须用于单好友手动发送")
        return 2
    if args.time_slot and args.source != "scheduled":
        print("错误: --time-slot 只能用于计划任务")
        return 2
    try:
        accounts = load_accounts()
    except ConfigError as exc:
        print(f"错误: {exc}")
        return 2
    if accounts is None:
        # 没有 config/accounts.json：旧单账号模式，行为不变（main 内部解析 --dry-run）。
        return run_single()
    if args.target is not None and not args.account:
        print("错误: 单好友发送必须指定账号")
        return 2
    try:
        return run_all_accounts(only=args.account)
    except KeyboardInterrupt:
        print("任务已取消")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

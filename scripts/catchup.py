"""Windows 定时检查入口；当前仅提供代码，不自动创建系统任务。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.catchup import check_once


if __name__ == "__main__":
    for account_id, status in check_once().items():
        print(f"{account_id}: {status}")

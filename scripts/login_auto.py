"""本地扫码登录辅助脚本（部署辅助，基于官方 scripts/login.py 改进）。

不需要回到终端按 Enter，轮询检测登录凭证 Cookie（sessionid 系列）成功后自动
保存 storage-state.json。修复：等待「登录」按钮渲染后再点击，确保扫码弹窗
弹出；每 8 秒截图一次到 login_debug/，便于诊断。请在项目根目录运行。

多账号用法：
    .\.venv\Scripts\python.exe scripts\login_auto.py --account account2
会读取 config/accounts.json 中该账号的 env 文件（.env.account2），把登录
状态保存到其 DOUYIN_STORAGE_STATE 指定路径（默认 storage-state-<id>.json），
并写回 env 文件。不传 --account 时为单账号模式，保存到 storage-state.json。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright


DOUYIN_URL = "https://www.douyin.com/"
LOGIN_WAIT_SECONDS = 600  # 最多等待 10 分钟完成扫码
POLL_INTERVAL_SECONDS = 8
SCREENSHOT_INTERVAL_SECONDS = 8
LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}
DEBUG_DIR = PROJECT_ROOT / "login_debug"


def _load_accounts() -> dict | None:
    path = PROJECT_ROOT / "config" / "accounts.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _resolve_account(account_id: str) -> tuple[Path, Path]:
    """返回 (env_file, storage_state_path)。env_file 必须存在。"""
    accounts = _load_accounts()
    if not accounts or not isinstance(accounts.get("accounts"), list):
        raise RuntimeError(f"config/accounts.json 不存在或格式错误，无法登录账号 {account_id}")
    item = None
    for acc in accounts["accounts"]:
        if isinstance(acc, dict) and acc.get("id") == account_id:
            item = acc
            break
    if item is None:
        raise RuntimeError(f"账号 {account_id} 不在 config/accounts.json 中")
    env_file = PROJECT_ROOT / str(item.get("env_file") or f".env.{account_id}")
    if not env_file.is_file():
        raise RuntimeError(f"账号环境文件不存在: {env_file}")
    storage = None
    try:
        from dotenv import dotenv_values
        storage = (dotenv_values(env_file) or {}).get("DOUYIN_STORAGE_STATE")
    except Exception:
        pass
    storage_path = PROJECT_ROOT / (storage or f"storage-state-{account_id}.json")
    return env_file, storage_path


def _write_env_key(env_file: Path, key: str, value: str) -> None:
    """把 key=value 写入 .env（保留注释与其他行）。"""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    if out and out[-1] != "":
        out.append("")
    env_file.write_text("\n".join(out), encoding="utf-8")


async def _extract_profile_avatar(page) -> str | None:
    """从已登录首页顶部区域取当前账号头像，不读取昵称、私信或其他内容。"""
    value = await page.evaluate("""() => {
      const width = window.innerWidth;
      return [...document.images].map(img => {
        const rect = img.getBoundingClientRect();
        const hint = `${img.className || ''} ${img.parentElement?.className || ''}`.toLowerCase();
        const isVisible = rect.width >= 20 && rect.height >= 20 && rect.top >= 0 && rect.top < 180 && rect.right > width * .55;
        const score = (hint.includes('avatar') ? 100 : 0) + (hint.includes('user') || hint.includes('profile') ? 30 : 0) + (isVisible ? 20 : 0);
        return {src: img.currentSrc || img.src || '', score, visible: isVisible};
      }).filter(item => item.visible && /^https?:\\/\\//.test(item.src)).sort((a, b) => b.score - a.score)[0]?.src || '';
    }""")
    return value if isinstance(value, str) and urlparse(value).scheme in {"http", "https"} else None


def _save_account_avatar(account_id: str | None, avatar_url: str | None) -> None:
    """仅为多账号当前条目保存头像 URL；失败不影响登录态保存。"""
    if not account_id or not avatar_url:
        return
    parsed = urlparse(avatar_url)
    if parsed.scheme not in {"http", "https"}:
        return
    accounts = _load_accounts()
    if not accounts or not isinstance(accounts.get("accounts"), list):
        return
    changed = False
    for account in accounts["accounts"]:
        if isinstance(account, dict) and account.get("id") == account_id:
            account["avatar_url"] = avatar_url
            changed = True
            break
    if changed:
        (PROJECT_ROOT / "config" / "accounts.json").write_text(
            json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _acquire_login_lock() -> bool:
    """锁文件防双开：已有 login_auto 实例在运行时返回 False（不杀进程）。

    注意：本环境的 venv python 由「launcher + 执行进程」两层组成，两者命令行
    都含 login_auto，任何按命令行匹配后杀进程的做法都可能误杀 launcher 而连带
    杀死自己，因此这里只用锁文件阻止重复启动，绝不 taskkill。
    """
    import os
    import subprocess
    lock = PROJECT_ROOT / "login_debug" / "login.lock"
    try:
        if lock.exists():
            old = lock.read_text(encoding="utf-8").strip()
            if old.isdigit():
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"if (Get-Process -Id {old} -ErrorAction SilentlyContinue) {{ 'alive' }} else {{ 'dead' }}"],
                    capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=10,
                )
                if "alive" in (r.stdout or ""):
                    print("已有登录窗口在运行（PID " + old + "），请先完成扫码或关闭旧窗口后重试", file=sys.stderr, flush=True)
                    return False
            lock.unlink(missing_ok=True)
        lock.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        # 锁不可用时放行，避免因文件问题阻塞登录
        return True


def _release_login_lock() -> None:
    """退出时删除锁文件。"""
    import os
    lock = PROJECT_ROOT / "login_debug" / "login.lock"
    try:
        cur = lock.read_text(encoding="utf-8").strip()
        if cur == str(os.getpid()):
            lock.unlink(missing_ok=True)
    except Exception:
        pass


async def main() -> None:
    parser = argparse.ArgumentParser(description="抖音扫码登录，自动保存登录状态")
    parser.add_argument("--account", default=None, help="多账号 id（如 account2）；缺省为单账号模式")
    args = parser.parse_args()
    if not _acquire_login_lock():
        raise RuntimeError("已有登录窗口在运行，请先完成扫码或关闭旧窗口后重试")
    try:
        await _run(args.account)
    finally:
        _release_login_lock()


async def _run(account_id: str | None) -> None:
    env_file: Path | None = None
    storage_path = PROJECT_ROOT / "storage-state.json"
    if account_id:
        env_file, storage_path = _resolve_account(account_id)
        print(f"[多账号] 登录账号 {account_id}，凭证将保存到 {storage_path.relative_to(PROJECT_ROOT)}")

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--start-maximized",
            ],
        )
        context = await browser.new_context(
            locale="zh-CN",
            viewport={"width": 1400, "height": 900},
        )
        page = await context.new_page()
        await page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        # 等页面渲染稳定后再尝试打开登录框
        await page.wait_for_timeout(3_000)
        opened = await _open_login(page)
        print("请在浏览器中扫码登录抖音。检测到登录成功后会自动保存登录状态。")
        print(f"登录框状态: {'已尝试打开' if opened else '未能打开，可手动点击页面右上角「登录」'}，等待超时 {LOGIN_WAIT_SECONDS // 60} 分钟。")

        deadline = asyncio.get_running_loop().time() + LOGIN_WAIT_SECONDS
        next_shot = asyncio.get_running_loop().time()
        logged_in = False
        while asyncio.get_running_loop().time() < deadline:
            if await _is_logged_in(page):
                logged_in = True
                break
            now = asyncio.get_running_loop().time()
            if now >= next_shot:
                next_shot = now + SCREENSHOT_INTERVAL_SECONDS
                try:
                    path = DEBUG_DIR / f"login-{datetime.now():%H%M%S}.png"
                    await page.screenshot(path=str(path))
                    print(f"[截图] {path}")
                except Exception as exc:
                    print(f"[截图失败] {exc}")
            await page.wait_for_timeout(POLL_INTERVAL_SECONDS * 1000)
        else:
            raise RuntimeError("等待扫码超时，请重新运行登录脚本")

        if not logged_in:
            raise RuntimeError("未检测到登录成功，请重新运行并完成扫码确认")

        try:
            _save_account_avatar(account_id, await _extract_profile_avatar(page))
        except Exception:
            # 头像仅为展示增强；抖音页面结构变化时不能影响登录与凭证保存。
            pass
        tmp = storage_path.with_name(storage_path.name + ".tmp")
        await context.storage_state(path=str(tmp))
        await browser.close()
        tmp.replace(storage_path)
        print(f"登录状态已保存到 {storage_path.relative_to(PROJECT_ROOT)}")
        if env_file is not None:
            _write_env_key(env_file, "DOUYIN_STORAGE_STATE", storage_path.name)
            print(f"已把 DOUYIN_STORAGE_STATE 写回 {env_file.relative_to(PROJECT_ROOT)}")


async def _open_login(page) -> bool:
    """点击右上角「登录」，再点击「扫码登录」，确保二维码弹出。"""
    login = page.get_by_text("登录", exact=True)
    clicked = False
    try:
        await login.first.wait_for(state="visible", timeout=20_000)
        await login.first.click(force=True)
        clicked = True
    except Exception:
        try:
            # 兜底：按位置点击右上角登录按钮
            await page.mouse.click(950, 42)
            clicked = True
        except Exception:
            pass
    await page.wait_for_timeout(2_500)

    qr_login = page.get_by_text("扫码登录", exact=True)
    try:
        await qr_login.first.wait_for(state="visible", timeout=8_000)
        await qr_login.first.click(force=True)
    except Exception:
        pass
    await page.wait_for_timeout(2_000)
    return clicked


async def _is_logged_in(page) -> bool:
    """登录成功的可靠信号：抖音登录凭证 Cookie（sessionid 系列）出现。"""
    try:
        cookies = await page.context.cookies(DOUYIN_URL)
        names = {c["name"] for c in cookies}
        if names & LOGIN_COOKIE_NAMES:
            return True
    except Exception:
        pass
    return False


if __name__ == "__main__":
    asyncio.run(main())

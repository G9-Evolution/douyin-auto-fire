# -*- coding: utf-8 -*-
"""爬取抖音私信表情面板（v2 完整版）。

用法：.\.venv\Scripts\python.exe scripts\scrape_stickers.py [--account account2] [--headful]

流程：打开抖音私信页 → 进入第一个会话 → 点开表情面板 →
遍历每个分类 tab（滚动加载全部表情）→ 收集 {分类, 名称, 顺序索引, 图片URL} →
下载表情缩略图到 assets/stickers/ → 输出 config/stickers_crawled.json
（含本地相对路径，供前端表情选择器与 config/stickers.json 合并使用）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.sync_api import sync_playwright

DEBUG = PROJECT_ROOT / "stickers_debug"
OUT_JSON = PROJECT_ROOT / "config" / "stickers_crawled.json"
ASSETS = PROJECT_ROOT / "assets" / "stickers"

PANEL_SELECTOR = ".componentsemojiemojiPanel"
TAB_SELECTOR = ".emojiEmojisModalTabsubTab"
ITEM_SELECTOR = ".emojiEmojiItememojiItem"


def resolve_state(account_id: str | None) -> Path:
    if account_id:
        try:
            from dotenv import dotenv_values
            env = dotenv_values(PROJECT_ROOT / f".env.{account_id}")
        except Exception:
            env = {}
        state = PROJECT_ROOT / (env.get("DOUYIN_STORAGE_STATE") or f"storage-state-{account_id}.json")
    else:
        state = PROJECT_ROOT / "storage-state.json"
    return state


def first_clickable(page, selectors: list[str], timeout_ms: int = 8000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click(force=True)
            return sel
        except Exception:
            continue
    return None


def collect_items(page) -> list[dict]:
    """滚动面板收集当前分类的全部表情项（名称/顺序/图片URL）。"""
    items: dict[str, dict] = {}
    order = 0
    seen = 0
    scroll_js = """
    (sel) => {
      const panel = document.querySelector(sel);
      if (!panel) return false;
      let sc = null;
      const cands = [panel, ...panel.querySelectorAll('*')];
      for (const el of cands) {
        if (el.scrollHeight > el.clientHeight + 8) { sc = el; break; }
      }
      if (!sc) return false;
      const before = sc.scrollTop;
      sc.scrollTop += sc.clientHeight * 0.9;
      return sc.scrollTop !== before;
    }
    """
    for _ in range(40):
        # 抓取当前可见表情
        data = page.evaluate(
            """(itemSel) => {
              const out = [];
              document.querySelectorAll(itemSel).forEach(el => {
                const desc = el.querySelector('.emojiEmojiItememojiItemDesc');
                const img = el.querySelector('img');
                out.push({
                  name: (desc ? desc.innerText : (el.getAttribute('aria-label') || el.getAttribute('title') || el.innerText || '')).trim(),
                  src: img ? img.getAttribute('src') : null
                });
              });
              return out;
            }""", ITEM_SELECTOR)
        for d in data:
            if not d.get("src"):
                continue
            key = d["src"].split("?")[0]
            if key not in items:
                items[key] = {"name": d.get("name") or f"表情{len(items)+1}", "src": d["src"], "order": order}
            order += 1
        # 滚动加载更多
        moved = page.evaluate(scroll_js, PANEL_SELECTOR)
        if not moved:
            break
        page.wait_for_timeout(600)
    return sorted(items.values(), key=lambda x: x["order"])


def download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
        data = urllib.request.urlopen(req, timeout=20).read()
        dest.write_bytes(data)
        return len(data) > 500
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="爬取抖音表情面板到本地")
    ap.add_argument("--account", default=None, help="账号 id（如 account2），缺省为单账号")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    state = resolve_state(args.account)
    if not state.is_file():
        raise SystemExit(f"凭证文件不存在: {state}")
    print(f"使用凭证: {state.relative_to(PROJECT_ROOT)}")

    DEBUG.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    categories: dict[str, list[dict]] = {}
    tab_labels: dict[int, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful)
        ctx = browser.new_context(storage_state=str(state), locale="zh-CN",
                                  viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        try:
            page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(5_000)
            first_clickable(page, ['a[href*="/chat/"]', '[class*="Conversation"]',
                                   '[class*="sessionList"] > div', '[class*="chatList"] > div > div'],
                            timeout_ms=6_000)
            page.wait_for_timeout(3_000)
            first_clickable(page, ['svg.messageMsgInputiconAction', 'button[aria-label*="表情"]',
                                   '[role="button"][aria-label*="表情"]', '[title*="表情"]'],
                            timeout_ms=6_000)
            page.wait_for_timeout(2_000)
            panel = page.locator(PANEL_SELECTOR).first
            if not panel.is_visible():
                raise SystemExit("表情面板未出现")
            page.wait_for_timeout(1_500)
            page.screenshot(path=str(DEBUG / f"{stamp}-panel.png"))

            tabs = page.locator(TAB_SELECTOR)
            n = tabs.count()
            print(f"发现 {n} 个分类 tab")
            for i in range(n):
                tab = tabs.nth(i)
                cls = (tab.get_attribute("class") or "")
                if "disabled" in cls:
                    print(f"  tab[{i}] 已禁用，跳过")
                    continue
                # tab 标识：优先 aria-label/title，其次截图比对前的 innerHTML 特征
                aria = tab.get_attribute("aria-label") or ""
                title = tab.get_attribute("title") or ""
                label = aria or title or f"分类{i+1}"
                tab_labels[i] = label
                tab.click(force=True)
                page.wait_for_timeout(1_000)
                collected = collect_items(page)
                if not collected:
                    print(f"  tab[{i}]「{label}」无表情项")
                    continue
                categories[label] = collected
                print(f"  tab[{i}]「{label}」收集 {len(collected)} 个表情")

            # 下载缩略图 + 生成最终数据（含本地相对路径）
            total = 0
            for cat, items in categories.items():
                for it in items:
                    ext = ".png" if ".png" in it["src"].split("?")[0] else ".webp"
                    fname = f"{cat}-{it['order']:03d}{ext}"
                    dest = ASSETS / fname
                    if not dest.exists() and not download(it["src"], dest):
                        print(f"  下载失败: {it['name']} ({it['src'][:60]}...)")
                        continue
                    it["img"] = f"assets/stickers/{fname}"
                    it["category"] = cat
                    total += 1
            print(f"共下载/引用 {total} 个表情缩略图")
            page.screenshot(path=str(DEBUG / f"{stamp}-done.png"))
        except Exception as exc:
            print(f"异常: {type(exc).__name__}: {exc}")
        finally:
            browser.close()

    OUT_JSON.write_text(json.dumps({
        "account": args.account or "default",
        "scraped_at": datetime.now().astimezone().isoformat(),
        "categories": categories,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"输出: {OUT_JSON.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()

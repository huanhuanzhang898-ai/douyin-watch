"""
抖音博主更新提醒：有新视频时推送到微信（PushPlus 或 Server酱）。
在 GitHub Actions 上定时运行，不需要安装抖音 App，也不需要开电脑。

需要的环境变量（在 GitHub 仓库的 Secrets 里设置）：
  DOUYIN_USERS    博主主页链接，多个用换行或英文逗号分隔
  DOUYIN_COOKIE   抖音网页版登录后的 Cookie
  PUSHPLUS_TOKEN  PushPlus 的 token（和下面二选一）
  SERVERCHAN_KEY  Server酱的 SendKey（和上面二选一）
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

STATE_FILE = "state.json"
CST = timezone(timedelta(hours=8))
MAX_AGE_HOURS = 48          # 只提醒最近 48 小时内发布的视频，避免博主置顶旧视频时误报
FAIL_ALERT_AFTER = 3        # 连续失败几次后提醒你（通常是 Cookie 过期）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


# ---------------- 推送 ----------------
def push(title, content):
    pp = os.getenv("PUSHPLUS_TOKEN", "").strip()
    sc = os.getenv("SERVERCHAN_KEY", "").strip()
    try:
        if pp:
            body = json.dumps({"token": pp, "title": title, "content": content,
                               "template": "markdown"}).encode()
            req = urllib.request.Request("https://www.pushplus.plus/send", data=body,
                                         headers={"Content-Type": "application/json"})
        elif sc:
            body = urllib.parse.urlencode({"title": title, "desp": content}).encode()
            req = urllib.request.Request(f"https://sctapi.ftqq.com/{sc}.send", data=body)
        else:
            print("[警告] 没有设置 PUSHPLUS_TOKEN 或 SERVERCHAN_KEY，只打印不推送")
            print(title, "\n", content)
            return
        with urllib.request.urlopen(req, timeout=20) as r:
            print("[推送]", title, "->", r.read().decode()[:200])
    except Exception as e:
        print("[推送失败]", e)


# ---------------- 状态 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------------- 抓取 ----------------
def parse_cookie(cookie_str):
    cookies = []
    for part in cookie_str.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            cookies.append({"name": name, "value": value, "domain": ".douyin.com", "path": "/"})
    return cookies


def fetch_user_videos(context, url):
    """打开博主主页，截获页面自己发出的作品列表请求。返回 (博主昵称, 视频列表)。"""
    page = context.new_page()
    captured = []

    def on_response(resp):
        if "/aweme/v1/web/aweme/post/" in resp.url:
            try:
                captured.append(resp.json())
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        for _ in range(25):                 # 最多等约 25 秒
            if captured:
                break
            page.wait_for_timeout(1000)
            if _ == 8:
                page.mouse.wheel(0, 1500)   # 滚一下，触发加载
        if not captured:
            page.screenshot(path="debug.png", full_page=False)
            return None, None
        videos, nickname = [], ""
        for data in captured:
            for a in data.get("aweme_list") or []:
                nickname = nickname or (a.get("author") or {}).get("nickname", "")
                videos.append({
                    "id": str(a.get("aweme_id")),
                    "desc": (a.get("desc") or "").strip(),
                    "time": int(a.get("create_time") or 0),
                })
        return nickname, videos
    finally:
        page.close()


def user_key(url):
    m = re.search(r"/user/([\w\-]+)", url)
    return m.group(1) if m else url


# ---------------- 主流程 ----------------
def main():
    users = [u.strip() for u in re.split(r"[\n,]", os.getenv("DOUYIN_USERS", "")) if u.strip()]
    cookie = os.getenv("DOUYIN_COOKIE", "").strip()
    if not users:
        print("请在 Secrets 里设置 DOUYIN_USERS")
        sys.exit(1)

    state = load_state()
    now = int(time.time())

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)   # 用 GitHub 自带的 Chrome
        except Exception:
            browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="zh-CN",
                                      viewport={"width": 1280, "height": 900})
        if cookie:
            context.add_cookies(parse_cookie(cookie))

        for url in users:
            key = user_key(url)
            s = state.setdefault(key, {"seen": [], "fails": 0, "alerted": False, "name": ""})
            try:
                nickname, videos = fetch_user_videos(context, url)
            except Exception as e:
                print("[出错]", url, e)
                nickname, videos = None, None

            if videos is None:
                s["fails"] += 1
                print(f"[失败] {url} 连续失败 {s['fails']} 次")
                if s["fails"] >= FAIL_ALERT_AFTER and not s["alerted"]:
                    push("抖音提醒：抓取失败",
                         f"博主 {s['name'] or url} 已连续 {s['fails']} 次没抓到数据。\n\n"
                         "最常见原因是 Cookie 过期：在电脑浏览器重新登录抖音网页版，"
                         "复制新的 Cookie，更新到 GitHub 的 DOUYIN_COOKIE 里即可。")
                    s["alerted"] = True
                continue

            s["fails"], s["alerted"] = 0, False
            s["name"] = nickname or s["name"]
            first_run = not s["seen"]
            new = [v for v in videos if v["id"] not in s["seen"]]

            if first_run:
                latest = max(videos, key=lambda v: v["time"]) if videos else None
                msg = f"已开始监控 **{s['name']}**，共读取到 {len(videos)} 个视频。"
                if latest:
                    t = datetime.fromtimestamp(latest["time"], CST).strftime("%m-%d %H:%M")
                    msg += f"\n\n最近一条（{t}）：{latest['desc'][:100] or '（无文案）'}"
                push(f"抖音监控已启动：{s['name']}", msg)
            else:
                for v in sorted(new, key=lambda v: v["time"]):
                    if now - v["time"] > MAX_AGE_HOURS * 3600:
                        continue
                    t = datetime.fromtimestamp(v["time"], CST).strftime("%m-%d %H:%M")
                    desc = v["desc"][:300] or "（无文案）"
                    push(f"{s['name']} 发新视频了",
                         f"**{s['name']}** · {t}\n\n{desc}\n\n"
                         f"[只看这一个视频](https://www.douyin.com/video/{v['id']})")

            s["seen"] = ([v["id"] for v in new] + s["seen"])[:100]
            print(f"[完成] {s['name']} 新视频 {len(new)} 个")

        browser.close()

    save_state(state)


if __name__ == "__main__":
    main()

"""
抖音博主更新提醒（带转写和操作提炼）
- 定时打开博主主页，截获作品列表
- 发现新视频：把音频交给阿里云百炼转成文字，再用通义千问提炼明确的买卖操作，推送到微信
- 每条视频的文字稿保存到 transcripts/ 目录

需要的环境变量（在 GitHub Secrets 里填）：
  DOUYIN_USERS      博主主页链接，多个用换行或逗号分隔
  DOUYIN_COOKIE     抖音网页版 Cookie
  PUSHPLUS_TOKEN    PushPlus 的 token（或用 SERVERCHAN_KEY）
  SERVERCHAN_KEY    Server酱的 SendKey（和上面二选一）
  DASHSCOPE_API_KEY 阿里云百炼 API Key（不填则只推送文案，不转写）
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
TRANSCRIPT_DIR = "transcripts"
CST = timezone(timedelta(hours=8))
MAX_AGE_HOURS = 48          # 只提醒最近 48 小时内发布的视频，避免博主置顶旧视频时误报
FAIL_ALERT_AFTER = 3        # 连续失败几次后提醒你（通常是 Cookie 过期）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

# 只监控这些博主（主页链接里 /user/ 后面那串）。留空 = 监控 DOUYIN_USERS 里的所有人
ONLY_USERS = {
    "MS4wLjABAAAApdql_In6mY-UvG0nEU6wqQo5GyYeIKTDHZr-MDWiK-lCV7Rpk_YvW_DnPwVRL2KS",  # 全职股民小海
}

DASHSCOPE = "https://dashscope.aliyuncs.com"
ASR_MODEL = "paraformer-v2"
LLM_MODEL = "qwen-plus"

EXTRACT_PROMPT = """你是股票短视频的笔记员。下面是抖音博主"{name}"一条视频的语音转写（机器转写，可能有错别字，请按上下文理解）和文案。

请严格按下面格式输出，不要加别的内容：

**一句话**：用一句话概括他这条视频说了什么（不超过40字）
**胆大的**：他给"胆大的/胆子大的/激进的/做超短的"人的明确操作
**他本人**：他本人的实际操作或明确计划（买入、卖出、继续持有、继续空仓）
**提到的标的**：他提到要买卖的具体标的或指数，没有就写"无"

规则：
1. 只写"明确的买卖操作 + 明确的时机"，例如"今天尾盘买入""明天高开或冲高就卖""科创50跌到1400点附近就买""明天创业和科创低开1.5%以上就买"。带条件的也要写，条件照原话写清楚。
2. 不要写：做T、"逢低可以买"这种没有具体时机的话、只对"没上车的人"说的话、泛泛的看法和情绪。
3. 某一项没有明确操作就写"无明确操作"。
4. 不要自己推测、不要补充他没说的内容。

文案：{desc}

转写：
{text}
"""


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


# ---------------- 阿里云百炼 ----------------
def ds_request(method, path, key, payload=None, extra_headers=None, timeout=30):
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(DASHSCOPE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def transcribe(audio_url, key, max_wait=240):
    """录音文件识别：把音频链接交给百炼，返回 (全文, 句子列表)。失败抛异常。"""
    task = ds_request("POST", "/api/v1/services/audio/asr/transcription", key,
                      {"model": ASR_MODEL, "input": {"file_urls": [audio_url]},
                       "parameters": {"language_hints": ["zh"]}},
                      {"X-DashScope-Async": "enable"})
    task_id = task["output"]["task_id"]
    t0 = time.time()
    while True:
        time.sleep(3)
        r = ds_request("GET", f"/api/v1/tasks/{task_id}", key)
        status = r["output"]["task_status"]
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise RuntimeError(f"转写任务失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        if time.time() - t0 > max_wait:
            raise RuntimeError("转写超时")
    res = r["output"]["results"][0]
    if res.get("subtask_status") != "SUCCEEDED":
        raise RuntimeError(f"转写子任务失败: {json.dumps(res, ensure_ascii=False)[:300]}")
    with urllib.request.urlopen(res["transcription_url"], timeout=30) as f:
        tr = json.loads(f.read().decode())
    sentences, texts = [], []
    for t in tr.get("transcripts", []):
        texts.append(t.get("text", ""))
        sentences += t.get("sentences", [])
    return "".join(texts).strip(), sentences


def extract_ops(name, desc, text, key):
    r = ds_request("POST", "/compatible-mode/v1/chat/completions", key,
                   {"model": LLM_MODEL, "temperature": 0.1,
                    "messages": [{"role": "user", "content": EXTRACT_PROMPT.format(
                        name=name, desc=desc or "（无）", text=text[:12000])}]},
                   timeout=90)
    return r["choices"][0]["message"]["content"].strip()


def analyze(video, name, key):
    """返回 (文字稿, 提炼结果, 用的音频来源)。全部失败时文字稿为 None，提炼结果为错误说明。"""
    last_err = "没有可用的音频地址"
    for label, url in video.get("audio_candidates", []):
        try:
            text, sentences = transcribe(url, key)
            if not text:
                last_err = f"{label} 没识别出文字"
                continue
            ops = extract_ops(name, video["desc"], text, key)
            return text, ops, label, sentences
        except Exception as e:
            last_err = f"{label}: {e}"
            print("[转写/提炼失败]", video["id"], last_err)
    return None, last_err, None, []


def save_transcript(name, video, text, ops, source, sentences):
    t = datetime.fromtimestamp(video["time"], CST)
    folder = os.path.join(TRANSCRIPT_DIR, re.sub(r'[\\/:*?"<>|\s]+', "_", name or "unknown"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{t:%Y-%m-%d_%H%M}_{video['id']}.md")
    lines = [f"# {name} · {t:%Y-%m-%d %H:%M}", "",
             f"原视频：https://www.douyin.com/video/{video['id']}", "",
             f"文案：{video['desc'] or '（无）'}", "",
             f"音频来源：{source}；机器转写（{ASR_MODEL}），未人工校对", "",
             "## 提炼", "", ops, "", "## 文字稿", ""]
    if sentences:
        for s in sentences:
            sec = int((s.get("begin_time") or 0) / 1000)
            lines.append(f"[{sec // 60:02d}:{sec % 60:02d}] {s.get('text', '')}")
    else:
        lines.append(text)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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


def first_url(obj):
    for u in (obj or {}).get("url_list") or []:
        if u:
            return u
    return None


def audio_candidates(a):
    """按优先级给出可交给转写的地址：作者原声 mp3 → 视频文件。"""
    cands = []
    author_uid = str((a.get("author") or {}).get("uid") or "")
    music = a.get("music") or {}
    murl = first_url(music.get("play_url"))
    is_original = (str(music.get("owner_id") or "") == author_uid and author_uid) \
        or "原声" in (music.get("title") or "")
    video = a.get("video") or {}
    vurl = first_url(video.get("play_addr")) or first_url(video.get("download_addr"))
    if murl and is_original:
        cands.append(("原声", murl))
    if vurl:
        cands.append(("视频", vurl))
    if murl and not is_original:
        cands.append(("背景音频", murl))
    return cands


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
                    "audio_candidates": audio_candidates(a),
                })
        return nickname, videos
    finally:
        page.close()


def user_key(url):
    m = re.search(r"/user/([\w\-]+)", url)
    return m.group(1) if m else url


def notify_new_video(name, v, key):
    t = datetime.fromtimestamp(v["time"], CST).strftime("%m-%d %H:%M")
    link = f"[看原视频](https://www.douyin.com/video/{v['id']})"
    desc = v["desc"][:200] or "（无文案）"
    if not key:
        push(f"{name} 发新视频了", f"**{name}** · {t}\n\n{desc}\n\n{link}")
        return
    text, ops, source, sentences = analyze(v, name, key)
    if text is None:
        push(f"{name} 发新视频了（转写失败）",
             f"**{name}** · {t}\n\n{desc}\n\n转写没成功：{ops[:200]}\n\n{link}")
        return
    save_transcript(name, v, text, ops, source, sentences)
    push(f"{name} · {t}",
         f"**{name}** · {t}\n\n{ops}\n\n文案：{desc}\n\n{link}")


# ---------------- 主流程 ----------------
def main():
    users = [u.strip() for u in re.split(r"[\n,]", os.getenv("DOUYIN_USERS", "")) if u.strip()]
    cookie = os.getenv("DOUYIN_COOKIE", "").strip()
    key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    test_latest = os.getenv("TEST_LATEST", "").strip() == "1"   # 手动测试：把每个博主最新一条走一遍转写
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
            k = user_key(url)
            if ONLY_USERS and k not in ONLY_USERS:
                print("[跳过]", k[:20], "不在 ONLY_USERS 名单里")
                continue
            s = state.setdefault(k, {"seen": [], "fails": 0, "alerted": False, "name": ""})
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

            if test_latest and videos:
                latest = max(videos, key=lambda v: v["time"])
                print(f"[测试] {s['name']} 最新一条 {latest['id']}，音频候选 {len(latest['audio_candidates'])} 个")
                notify_new_video(s["name"], latest, key)
            elif first_run:
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
                    notify_new_video(s["name"], v, key)

            s["seen"] = ([v["id"] for v in new] + s["seen"])[:100]
            print(f"[完成] {s['name']} 新视频 {len(new)} 个")

        browser.close()

    save_state(state)


if __name__ == "__main__":
    main()

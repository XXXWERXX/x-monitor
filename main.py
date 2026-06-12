#!/usr/bin/env python3
"""
X (Twitter) 推文监控 + 翻译精炼 + 邮件转发
============================================
Playwright 真浏览器抓取 → 时间戳去重取最新 → 英译中 → 精炼 → QQ邮箱

特性:
  · 只发最新一条推文
  · 英文自动翻译成中文
  · AI精炼提取关键信息
  · UTC+8 北京时间
  · 每10分钟检查一次
"""

import argparse
import json
import os
import re
import smtplib
import sys
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

# ── Windows UTF-8 ──
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 加载 .env ──
def _load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ══════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════

TARGET_SCREEN_NAME = os.getenv("TARGET_SCREEN_NAME", "elonmusk")
MAX_TWEETS_FETCH = 5  # 每次只抓最新5条（减少页面处理时间）

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
COOKIES_FILE = DATA_DIR / "x_cookies.json"
STATE_FILE = DATA_DIR / "monitor_state.json"  # 改为存最新推文时间

HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
VERBOSE = os.getenv("VERBOSE", "0") == "1"

# UTC+8 时区
TZ_BEIJING = timezone(timedelta(hours=8))


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if VERBOSE:
        log(msg, "DEBUG")


def now_beijing() -> str:
    return datetime.now(TZ_BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def now_beijing_iso() -> str:
    return datetime.now(TZ_BEIJING).isoformat()


# ══════════════════════════════════════════════
#  翻译 & 精炼
# ══════════════════════════════════════════════

def translate_en_to_cn(text: str) -> str:
    """Google Translate 免费 API（无需 Key）"""
    if not text or not text.strip():
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": "zh-CN",
            "dt": "t",
            "q": text[:1500],  # 限制长度防止超时
        }
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            # 拼接所有翻译片段
            parts = []
            for segment in data[0]:
                if segment[0]:
                    parts.append(segment[0])
            return "".join(parts)
        else:
            log(f"翻译 API HTTP {resp.status_code}", "WARN")
            return ""
    except Exception as e:
        log(f"翻译失败: {e}", "WARN")
        return ""


def refine_text(text: str) -> str:
    """
    精炼推文: 提取关键信息
    1. 去掉 URL
    2. 去掉 @mention
    3. 去掉多余空白
    4. 提取核心句子
    """
    # 去掉 t.co 链接
    text = re.sub(r'https?://t\.co/\S+', '', text)
    # 去掉其他 URL
    text = re.sub(r'https?://\S+', '', text)
    # 去掉 @mention
    text = re.sub(r'@\w+', '', text)
    # 去掉多余空白
    text = re.sub(r'\s+', ' ', text).strip()
    # 去掉多余换行
    text = re.sub(r'\n+', '\n', text).strip()
    return text


def summarize(text_cn: str) -> str:
    """
    从翻译结果中提取关键信息。
    简单策略: 取前几句作为核心内容。
    """
    if not text_cn:
        return ""
    # 按句号/感叹号/问号分句
    sentences = re.split(r'[。！？!?\n]', text_cn)
    key_sentences = [s.strip() for s in sentences if len(s.strip()) > 3]
    if not key_sentences:
        return text_cn[:200]
    # 取前2-3句
    return "。".join(key_sentences[:3]) + "。"


def analyze_tweet(text_en: str) -> dict:
    """完整分析: 原文清理 → 翻译 → 精炼"""
    cleaned = refine_text(text_en)
    translated = translate_en_to_cn(cleaned) if cleaned else ""
    key_point = summarize(translated) if translated else ""
    return {
        "cleaned": cleaned,
        "translated": translated,
        "key_point": key_point,
    }


# ══════════════════════════════════════════════
#  Playwright 抓取
# ══════════════════════════════════════════════

def _load_cookies_json() -> Optional[list]:
    env_cookies = os.getenv("X_COOKIES", "")
    if env_cookies:
        try:
            return json.loads(env_cookies)
        except json.JSONDecodeError:
            pass
    if COOKIES_FILE.exists():
        try:
            return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def _cookies_for_playwright(cookie_list: list) -> list[dict]:
    result = []
    for c in cookie_list:
        pw = {"name": c["name"], "value": c["value"],
              "domain": c.get("domain", ".x.com"), "path": c.get("path", "/")}
        if c.get("httpOnly"):
            pw["httpOnly"] = True
        if c.get("secure"):
            pw["secure"] = True
        same_site = c.get("sameSite", "")
        if same_site and same_site != "no_restriction":
            pw["sameSite"] = same_site.replace("_", "").title()
        result.append(pw)
    return result


def fetch_tweets(screen_name: str) -> list[dict]:
    """Playwright 抓取，只取最新几条"""
    mode = "cookies" if (_load_cookies_json()) else "nocookies"
    log(f"抓取 @{screen_name} ({mode})")

    proxy_config = None
    proxy_str = HTTPS_PROXY or HTTP_PROXY or ""
    if proxy_str:
        proxy_config = {"server": proxy_str}

    tweets = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
            proxy=proxy_config if proxy_config else None,
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        cookies = _load_cookies_json()
        if cookies:
            context.add_cookies(_cookies_for_playwright(cookies))

        page = context.new_page()

        try:
            url = f"https://x.com/{screen_name}"
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)  # 等 JS 渲染

            try:
                page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
            except PlaywrightTimeout:
                debug("无推文元素")
                browser.close()
                return []

            # 只取前几个推文元素（页面按时间倒序排列）
            elements = page.query_selector_all('[data-testid="tweet"]')[:MAX_TWEETS_FETCH]

            for el in elements:
                t = _extract_tweet(el)
                if t:
                    tweets.append(t)

        except PlaywrightTimeout:
            log("页面加载超时", "WARN")
        except Exception as e:
            log(f"抓取异常: {e}", "ERROR")
            debug(traceback.format_exc())
        finally:
            browser.close()

    log(f"抓取到 {len(tweets)} 条推文")
    return tweets


def _extract_tweet(el) -> Optional[dict]:
    """从 DOM 元素提取单条推文"""
    links = el.query_selector_all('a[href*="/status/"]')
    tid = ""
    tweet_url = ""
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/status/(\d+)", href)
        if m:
            tid = m.group(1)
            tweet_url = f"https://x.com{href.split('?')[0]}"
            break
    if not tid:
        return None

    text_el = el.query_selector('[data-testid="tweetText"]')
    text = text_el.inner_text() if text_el else ""

    time_el = el.query_selector("time")
    created_at = time_el.get_attribute("datetime") if time_el else ""

    def _count(label):
        el2 = el.query_selector(f'[data-testid="{label}"]')
        if el2:
            aria = el2.get_attribute("aria-label") or ""
            m = re.search(r"(\d[\d,]*)", aria)
            if m:
                return int(m.group(1).replace(",", ""))
        return 0

    return {
        "id": tid,
        "created_at": created_at,
        "full_text": text,
        "favorite_count": _count("like"),
        "retweet_count": _count("retweet"),
        "reply_count": _count("reply"),
        "view_count": "N/A",
        "author_name": TARGET_SCREEN_NAME,
        "author_screen_name": TARGET_SCREEN_NAME,
        "url": tweet_url or f"https://x.com/{TARGET_SCREEN_NAME}/status/{tid}",
    }


# ══════════════════════════════════════════════
#  时间戳去重
# ══════════════════════════════════════════════

def load_state() -> dict:
    """加载监控状态: 上次最新推文的时间戳"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {"last_tweet_time": "", "last_tweet_id": "", "updated_at": ""}


def save_state(tweet_time: str, tweet_id: str) -> None:
    """保存最新推文时间戳"""
    STATE_FILE.write_text(json.dumps({
        "last_tweet_time": tweet_time,
        "last_tweet_id": tweet_id,
        "updated_at": now_beijing_iso(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def find_newest_new_tweet(tweets: list[dict]) -> Optional[dict]:
    """
    对比历史状态，找到比上次更新的推文。
    返回最新的一条（如果有比历史记录更新的）。
    """
    if not tweets:
        return None

    state = load_state()
    last_time = state.get("last_tweet_time", "")

    # 推文已按页面顺序排列（通常最新在前），按 created_at 排序
    sorted_tweets = sorted(tweets, key=lambda t: t.get("created_at", ""), reverse=True)

    newest = sorted_tweets[0]  # 当前最新

    current_time = newest.get("created_at", "")
    current_id = newest.get("id", "")

    # 如果和上次一样，说明没有新推文
    if current_id == state.get("last_tweet_id", ""):
        log(f"最新推文未变 ({current_id})")
        return None

    if last_time and current_time <= last_time:
        log(f"没有比 {last_time} 更新的推文")
        return None

    log(f"发现新推文! 时间: {current_time}")
    return newest


def mark_as_sent(tweet: dict) -> None:
    """标记此推文为已发送"""
    save_state(tweet.get("created_at", ""), tweet.get("id", ""))


# ══════════════════════════════════════════════
#  邮件
# ══════════════════════════════════════════════

def send_email(subject: str, html_body: str) -> bool:
    recipients_str = RECIPIENT_EMAIL
    recipients = [e.strip() for e in recipients_str.split(",") if e.strip()]
    sender = SENDER_EMAIL
    password = SENDER_PASSWORD

    if not sender or not password or not recipients:
        log("邮件配置缺失", "ERROR")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", html_body).strip(), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
                s.login(sender, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
                s.ehlo(); s.starttls(); s.ehlo()
                s.login(sender, password)
                s.send_message(msg)
        log(f"邮件已发送 -> {', '.join(recipients)}")
        return True
    except Exception as e:
        log(f"邮件失败: {e}", "ERROR")
        return False


# ══════════════════════════════════════════════
#  邮件模板（单条推文 + 翻译 + 精炼）
# ══════════════════════════════════════════════

def build_single_tweet_email(tweet: dict, analysis: dict, target: str) -> str:
    """为单条最新推文构建邮件"""
    text_en = tweet.get("full_text", "")
    cleaned = analysis.get("cleaned", "")
    translated = analysis.get("translated", "")
    key_point = analysis.get("key_point", "")

    # 时间格式化
    created_str = tweet.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        dt_bj = dt.astimezone(TZ_BEIJING)
        time_str = dt_bj.strftime("%Y年%m月%d日 %H:%M:%S") + " (北京时间)"
    except (ValueError, AttributeError):
        time_str = created_str or "未知"

    stats = (
        f"💬{tweet.get('reply_count',0):,}  "
        f"🔁{tweet.get('retweet_count',0):,}  "
        f"❤️{tweet.get('favorite_count',0):,}"
    )

    html = f"""
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:560px;margin:20px auto;">

  <!-- 头部 -->
  <div style="background:linear-gradient(135deg,#1d9bf0,#0c7abf);padding:20px 24px;border-radius:16px 16px 0 0;">
    <h1 style="margin:0;font-size:20px;color:#fff;">🐦 @{target} 发新帖了</h1>
    <p style="margin:6px 0 0;font-size:13px;color:rgba(255,255,255,0.85);">{time_str}</p>
  </div>

  <!-- 原文 -->
  <div style="background:#fff;padding:20px 24px;border-left:1px solid #e1e8ed;border-right:1px solid #e1e8ed;">
    <div style="margin-bottom:6px;font-size:11px;font-weight:600;color:#8899a6;text-transform:uppercase;letter-spacing:1px;">📝 英文原文</div>
    <div style="font-size:15px;line-height:1.7;color:#0f1419;margin-bottom:14px;white-space:pre-wrap;">{_escape(text_en)}</div>
    <div style="font-size:12px;color:#536471;margin-bottom:12px;">{stats}</div>
    <a href="{tweet.get('url','#')}" style="display:inline-block;padding:8px 20px;background:#1d9bf0;color:#fff;text-decoration:none;border-radius:20px;font-size:13px;">🔗 查看原文</a>
  </div>"""

    if translated:
        html += f"""
  <!-- 中文翻译 -->
  <div style="background:#f8fafc;padding:20px 24px;border-left:1px solid #e1e8ed;border-right:1px solid #e1e8ed;">
    <div style="margin-bottom:6px;font-size:11px;font-weight:600;color:#8899a6;text-transform:uppercase;letter-spacing:1px;">🌐 中文翻译</div>
    <div style="font-size:15px;line-height:1.7;color:#0f1419;white-space:pre-wrap;">{_escape(translated)}</div>
  </div>"""

    if key_point and key_point != translated:
        html += f"""
  <!-- 精炼要点 -->
  <div style="background:#fffbeb;padding:20px 24px;border-left:3px solid #f59e0b;border-right:1px solid #e1e8ed;">
    <div style="margin-bottom:6px;font-size:11px;font-weight:600;color:#b45309;text-transform:uppercase;letter-spacing:1px;">💡 核心要点</div>
    <div style="font-size:14px;line-height:1.7;color:#78350f;">{_escape(key_point)}</div>
  </div>"""

    html += f"""
  <!-- 底部 -->
  <div style="background:#f7f9fa;padding:14px 24px;border:1px solid #e1e8ed;border-radius:0 0 16px 16px;text-align:center;">
    <p style="margin:0;font-size:11px;color:#8899a6;">X Monitor Bot · 每10分钟检查 · GitHub Actions · 免费运行</p>
    <p style="margin:4px 0 0;font-size:11px;color:#8899a6;">本次检查: {now_beijing()} (北京时间)</p>
  </div>

</div>
</body></html>"""
    return html


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ══════════════════════════════════════════════
#  主逻辑
# ══════════════════════════════════════════════

def check_and_notify() -> dict:
    summary = {
        "target": TARGET_SCREEN_NAME,
        "fetched": 0,
        "is_new": False,
        "email_sent": False,
        "time": now_beijing(),
    }

    # 1. 抓取
    tweets = fetch_tweets(TARGET_SCREEN_NAME)
    summary["fetched"] = len(tweets)

    if not tweets:
        return summary

    # 2. 找最新一条新推文
    newest = find_newest_new_tweet(tweets)

    if not newest:
        return summary

    summary["is_new"] = True
    summary["new_tweet_id"] = newest.get("id", "")

    # 3. 翻译 + 精炼
    log("翻译及精炼中...")
    analysis = analyze_tweet(newest.get("full_text", ""))

    # 4. 构建邮件并发送
    html = build_single_tweet_email(newest, analysis, TARGET_SCREEN_NAME)
    subject = f"[X监控] @{TARGET_SCREEN_NAME} 发帖了 — {now_beijing()}"

    if send_email(subject, html):
        summary["email_sent"] = True
        # 5. 更新状态，标记已发送
        mark_as_sent(newest)
        log("✓ 新推文已发送")
    else:
        log("✗ 邮件发送失败", "ERROR")

    return summary


def interactive_login():
    print("\n" + "=" * 60)
    print("  X Cookies 获取向导")
    print("=" * 60)
    print("1. 浏览器登录 https://x.com\n2. Cookie-Editor 导出 JSON\n3. 粘贴到下方\n")
    lines = []
    try:
        while True:
            lines.append(input())
    except (EOFError, KeyboardInterrupt):
        pass
    try:
        cookies = json.loads("\n".join(lines))
        if isinstance(cookies, dict):
            cookies = [cookies]
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已保存 {len(cookies)} 个 cookies")
    except json.JSONDecodeError as e:
        print(f"JSON 错误: {e}")


def main():
    parser = argparse.ArgumentParser(description="X 推文监控 + 翻译精炼")
    parser.add_argument("--login", action="store_true", help="获取 cookies")
    parser.add_argument("--test-email", action="store_true", help="测试邮件")
    parser.add_argument("--test-translate", action="store_true", help="测试翻译")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--target", type=str, default="", help="临时目标")
    args = parser.parse_args()

    global VERBOSE, TARGET_SCREEN_NAME
    if args.verbose:
        VERBOSE = True
    if args.target:
        TARGET_SCREEN_NAME = args.target

    if args.login:
        interactive_login()
        return

    if args.test_translate:
        test_text = "BREAKING: Just launched the new AI model that will change everything. Here's why it matters — a thread 🧵"
        print(f"原文: {test_text}")
        result = analyze_tweet(test_text)
        print(f"清理: {result['cleaned']}")
        print(f"翻译: {result['translated']}")
        print(f"精炼: {result['key_point']}")
        return

    if args.test_email:
        print("测试邮件...")
        analysis = analyze_tweet("Just shipped a new feature. Check it out!")
        dummy_tweet = {
            "id": "000000",
            "created_at": datetime.now(TZ_BEIJING).isoformat(),
            "full_text": "Just shipped a new feature. Check it out!",
            "favorite_count": 42, "retweet_count": 10, "reply_count": 5,
            "url": "https://x.com/test/status/000000",
        }
        html = build_single_tweet_email(dummy_tweet, analysis, TARGET_SCREEN_NAME)
        ok = send_email(f"[X监控] 测试邮件 - {now_beijing()}", html)
        print("成功!" if ok else "失败!")
        return

    print(f"\n{'='*50}\n  X 推文监控\n  目标: @{TARGET_SCREEN_NAME}\n  时间: {now_beijing()}\n{'='*50}\n")
    try:
        r = check_and_notify()
        print("\n" + json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log(err, "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()

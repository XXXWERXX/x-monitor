#!/usr/bin/env python3
"""
X (Twitter) 推文监控 + 邮件转发（Playwright 真浏览器版）
========================================================
用 Playwright 启动真实 Chromium 浏览器访问 X.com，
从页面 DOM 中提取推文，X 无法区分机器人和真人。
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
MAX_TWEETS = int(os.getenv("MAX_TWEETS_PER_CHECK", "10"))

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")

DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
COOKIES_FILE = DATA_DIR / "x_cookies.json"
SENT_TWEETS_FILE = DATA_DIR / "sent_tweets.json"

HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")
VERBOSE = os.getenv("VERBOSE", "0") == "1"


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if VERBOSE:
        log(msg, "DEBUG")


# ══════════════════════════════════════════════
#  Playwright 浏览器抓取
# ══════════════════════════════════════════════

def _load_cookies_json() -> Optional[list]:
    """加载 cookies JSON"""
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
    """将 Cookie-Editor 格式转为 Playwright 格式"""
    result = []
    for c in cookie_list:
        pw_cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".x.com"),
            "path": c.get("path", "/"),
        }
        # Playwright 需要 httpOnly 和 secure
        if c.get("httpOnly"):
            pw_cookie["httpOnly"] = True
        if c.get("secure"):
            pw_cookie["secure"] = True
        # sameSite
        same_site = c.get("sameSite", "")
        if same_site and same_site != "no_restriction":
            pw_cookie["sameSite"] = same_site.replace("_", "").title()
        result.append(pw_cookie)
    return result


def fetch_tweets(screen_name: str) -> list[dict]:
    """用 Playwright 真浏览器抓取推文"""
    log(f"启动浏览器抓取 @{screen_name}...")

    tweets = []
    proxy_config = None
    proxy_str = HTTPS_PROXY or HTTP_PROXY or ""
    if proxy_str:
        proxy_config = {"server": proxy_str}

    with sync_playwright() as p:
        # 启动 Chromium
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            proxy=proxy_config if proxy_config else None,
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        # 加载 cookies
        cookies = _load_cookies_json()
        if cookies:
            pw_cookies = _cookies_for_playwright(cookies)
            context.add_cookies(pw_cookies)
            log(f"已注入 {len(pw_cookies)} 个 cookies")

        page = context.new_page()

        try:
            # 访问用户主页
            url = f"https://x.com/{screen_name}"
            log(f"访问 {url}")

            page.goto(url, wait_until="networkidle", timeout=30000)
            log(f"页面加载完成: {page.title()}")

            # 等待推文出现
            try:
                page.wait_for_selector('[data-testid="tweet"]', timeout=10000)
            except PlaywrightTimeout:
                # 如果等了10秒还没推文，截图看下
                page.screenshot(path=str(DATA_DIR / "debug_page.png"))
                log("推文未出现，可能是被拦截或用户无推文", "WARN")
                # 尝试获取页面内容看看
                content = page.content()
                debug(f"页面片段: {content[:2000]}")
                browser.close()
                return []

            # 滚动几次加载更多推文
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 800)")
                page.wait_for_timeout(1000)

            # 提取推文
            tweet_elements = page.query_selector_all('[data-testid="tweet"]')
            log(f"找到 {len(tweet_elements)} 个推文元素")

            for el in tweet_elements[:MAX_TWEETS]:
                try:
                    tweet_data = _extract_tweet_from_element(el, page)
                    if tweet_data:
                        tweets.append(tweet_data)
                except Exception as e:
                    debug(f"提取单条推文失败: {e}")

        except PlaywrightTimeout:
            log("页面加载超时", "ERROR")
            try:
                page.screenshot(path=str(DATA_DIR / "debug_timeout.png"))
            except Exception:
                pass
        except Exception as e:
            log(f"浏览器抓取失败: {e}", "ERROR")
            debug(traceback.format_exc())
            try:
                page.screenshot(path=str(DATA_DIR / "debug_error.png"))
            except Exception:
                pass
        finally:
            browser.close()

    log(f"共提取 {len(tweets)} 条推文")
    return tweets


def _extract_tweet_from_element(el, page) -> Optional[dict]:
    """从推文 DOM 元素提取数据"""
    # 提取推文链接 (含 ID)
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

    # 提取推文文本
    text = ""
    text_el = el.query_selector('[data-testid="tweetText"]')
    if text_el:
        text = text_el.inner_text()

    # 提取时间
    time_el = el.query_selector("time")
    created_at = ""
    if time_el:
        created_at = time_el.get_attribute("datetime") or ""

    # 提取互动数据
    def _get_count(label: str) -> int:
        """从 aria-label 中提取数字"""
        el_sel = el.query_selector(f'[data-testid="{label}"]')
        if el_sel:
            aria = el_sel.get_attribute("aria-label") or ""
            m = re.search(r"(\d[\d,]*)", aria)
            if m:
                return int(m.group(1).replace(",", ""))
        return 0

    return {
        "id": tid,
        "created_at": created_at,
        "full_text": text,
        "favorite_count": _get_count("like"),
        "retweet_count": _get_count("retweet"),
        "reply_count": _get_count("reply"),
        "quote_count": 0,
        "view_count": "N/A",
        "author_name": TARGET_SCREEN_NAME,
        "author_screen_name": TARGET_SCREEN_NAME,
        "url": tweet_url or f"https://x.com/{TARGET_SCREEN_NAME}/status/{tid}",
    }


# ══════════════════════════════════════════════
#  邮件
# ══════════════════════════════════════════════

def send_email(subject: str, html_body: str, to_email: str = "") -> bool:
    recipient = to_email or RECIPIENT_EMAIL
    sender = SENDER_EMAIL
    password = SENDER_PASSWORD

    if not sender or not password or not recipient:
        log("邮件配置缺失", "ERROR")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
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
        log(f"邮件已发送 -> {recipient}")
        return True
    except Exception as e:
        log(f"邮件失败: {e}", "ERROR")
        return False


# ══════════════════════════════════════════════
#  去重
# ══════════════════════════════════════════════

def load_sent() -> set[str]:
    if SENT_TWEETS_FILE.exists():
        try:
            data = json.loads(SENT_TWEETS_FILE.read_text(encoding="utf-8"))
            return set((data.get("tweet_ids", []) if isinstance(data, dict) else data)[-500:])
        except Exception:
            pass
    return set()


def save_sent(ids: set[str]) -> None:
    SENT_TWEETS_FILE.write_text(json.dumps({
        "tweet_ids": list(ids)[-500:],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def deduplicate(tweets: list[dict]) -> list[dict]:
    sent = load_sent()
    new_list = []
    for t in tweets:
        tid = t.get("id", "")
        if tid and tid not in sent:
            new_list.append(t)
            sent.add(tid)
    if new_list:
        save_sent(sent)
        log(f"{len(new_list)} 条新 / {len(tweets)} 条总")
    else:
        log("无新推文")
    return new_list


# ══════════════════════════════════════════════
#  邮件模板
# ══════════════════════════════════════════════

def build_email(tweets: list[dict], target: str) -> str:
    utc8 = timezone(timedelta(hours=8))
    cards = ""
    for t in tweets:
        text = (t.get("full_text") or "").replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        text = re.sub(r'(https?://\S+)', r'<a href="\1" style="color:#1d9bf0;">\1</a>', text)
        ts = (t.get("created_at") or "")[:19]
        cards += f"""
        <div style="margin-bottom:14px;padding:14px;border:1px solid #e1e8ed;border-radius:12px;background:#fff;">
            <div style="font-size:15px;line-height:1.5;color:#0f1419;margin-bottom:6px;">{text}</div>
            <div style="font-size:12px;color:#536471;">{ts}</div>
            <div style="font-size:12px;color:#536471;margin-top:4px;">
                💬{t.get('reply_count',0):,} 🔁{t.get('retweet_count',0):,} ❤️{t.get('favorite_count',0):,}
            </div>
            <div style="margin-top:8px;">
                <a href="{t.get('url','#')}" style="display:inline-block;padding:5px 14px;background:#1d9bf0;color:#fff;text-decoration:none;border-radius:20px;font-size:12px;">查看原文</a>
            </div>
        </div>"""
    return f"""
    <html><body style="margin:0;padding:18px;background:#f7f9fa;font-family:-apple-system,sans-serif;">
    <div style="max-width:600px;margin:0 auto;">
        <div style="background:#1d9bf0;padding:18px 24px;border-radius:12px 12px 0 0;color:#fff;">
            <h2 style="margin:0;">X 推文提醒</h2>
            <p style="margin:4px 0 0;font-size:13px;">@{target} · {len(tweets)} 条新推文</p>
        </div>
        <div style="background:#fff;padding:18px;border:1px solid #e1e8ed;">{cards}</div>
        <div style="background:#f7f9fa;padding:10px;text-align:center;font-size:11px;color:#8899a6;border:1px solid #e1e8ed;border-radius:0 0 12px 12px;">
            X Monitor Bot · GitHub Actions · 免费
        </div>
    </div></body></html>"""


# ══════════════════════════════════════════════
#  主逻辑
# ══════════════════════════════════════════════

def check_and_notify() -> dict:
    summary = {"target": TARGET_SCREEN_NAME, "fetched": 0, "new": 0, "email_sent": False}
    tweets = fetch_tweets(TARGET_SCREEN_NAME)
    summary["fetched"] = len(tweets)
    if tweets:
        tweets.sort(key=lambda t: t.get("id", ""), reverse=True)
    new_tweets = deduplicate(tweets)
    summary["new"] = len(new_tweets)
    if new_tweets:
        html = build_email(new_tweets, TARGET_SCREEN_NAME)
        subject = f"[X监控] @{TARGET_SCREEN_NAME} 发布了 {len(new_tweets)} 条新推文"
        if send_email(subject, html):
            summary["email_sent"] = True
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
    parser = argparse.ArgumentParser(description="X 推文监控 + 邮件转发 (Playwright版)")
    parser.add_argument("--login", action="store_true", help="获取 cookies")
    parser.add_argument("--test-email", action="store_true", help="测试邮件")
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
    if args.test_email:
        print("测试邮件...")
        ok = send_email("[X监控] 测试", f"<h2>测试</h2><p>@{TARGET_SCREEN_NAME}</p><p>{datetime.now()}</p>")
        print("成功!" if ok else "失败!")
        return

    print(f"\n{'='*50}\n  X 推文监控 (Playwright)\n  目标: @{TARGET_SCREEN_NAME}\n{'='*50}\n")
    try:
        r = check_and_notify()
        print("\n" + json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log(err, "ERROR")
        sys.exit(1)


if __name__ == "__main__":
    main()

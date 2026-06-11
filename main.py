#!/usr/bin/env python3
"""
X (Twitter) 推文监控 + 邮件转发
========================================
定时抓取指定博主的推文，检测到新推文时通过 QQ 邮箱 SMTP 发送邮件。
设计为在 GitHub Actions 上运行（境外 IP，无需 VPN），零成本。

使用方式:
  1. 本地首次运行获取 cookies:  python main.py --login
  2. GitHub Actions 定时运行:     python main.py
"""

import argparse
import asyncio
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

# ── 修复 Windows 终端 GBK 编码 ──
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 加载 .env 文件 ──
def _load_dotenv():
    """从 .env 文件加载环境变量"""
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

from twikit import Client

# ══════════════════════════════════════════════
#  配置区
# ══════════════════════════════════════════════

TARGET_SCREEN_NAME = os.getenv("TARGET_SCREEN_NAME", "elonmusk")
MAX_TWEETS_PER_CHECK = int(os.getenv("MAX_TWEETS_PER_CHECK", "10"))

# ---- 邮件（QQ 邮箱） ----
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")

# ---- 运行时 ----
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
COOKIES_FILE = DATA_DIR / "x_cookies.json"
SENT_TWEETS_FILE = DATA_DIR / "sent_tweets.json"

# ---- 代理（本地用） ----
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

# ---- 日志 ----
VERBOSE = os.getenv("VERBOSE", "0") == "1"


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if VERBOSE:
        log(msg, "DEBUG")


# ══════════════════════════════════════════════
#  X 推文抓取（twikit）
# ══════════════════════════════════════════════

def _build_twikit_client() -> Client:
    """构建 twikit 客户端，加载 cookies"""
    proxy = HTTPS_PROXY or HTTP_PROXY or None
    client = Client(language="en-US", proxy=proxy)

    # 加载 cookies
    cookie_list = None

    env_cookies = os.getenv("X_COOKIES", "")
    if env_cookies:
        try:
            cookie_list = json.loads(env_cookies)
            log(f"从环境变量加载了 {len(cookie_list)} 个 cookies")
        except json.JSONDecodeError:
            log("X_COOKIES 格式错误", "WARN")

    if cookie_list is None and COOKIES_FILE.exists():
        try:
            cookie_list = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            log(f"从文件加载了 {len(cookie_list)} 个 cookies")
        except (json.JSONDecodeError, KeyError):
            log("Cookies 文件格式错误", "ERROR")

    if cookie_list:
        # twikit 的 set_cookies 接受 dict 格式
        cookies_dict = {}
        for c in cookie_list:
            cookies_dict[c["name"]] = c["value"]
        client.set_cookies(cookies_dict)
        log(f"已设置 {len(cookies_dict)} 个 cookies")
    else:
        log("⚠️ 未找到 cookies，将以访客模式访问", "WARN")

    return client


async def fetch_tweets_async(screen_name: str, max_count: int = MAX_TWEETS_PER_CHECK) -> list[dict]:
    """异步抓取推文"""
    log(f"开始抓取 @{screen_name} 的最新推文...")

    client = _build_twikit_client()

    try:
        # 获取用户
        user = await client.get_user_by_screen_name(screen_name)
        log(f"找到用户: @{user.screen_name} (ID: {user.id})")

        # 获取推文
        tweets_data = await user.get_tweets("Tweets", count=max_count)

        result = []
        for tweet in tweets_data:
            result.append({
                "id": tweet.id,
                "created_at": str(tweet.created_at) if tweet.created_at else "",
                "full_text": tweet.text or "",
                "favorite_count": getattr(tweet, "favorite_count", 0),
                "retweet_count": getattr(tweet, "retweet_count", 0),
                "reply_count": getattr(tweet, "reply_count", 0),
                "quote_count": getattr(tweet, "quote_count", 0),
                "view_count": getattr(tweet, "view_count", "N/A"),
                "author_name": user.name,
                "author_screen_name": user.screen_name,
                "url": f"https://x.com/{user.screen_name}/status/{tweet.id}",
            })

        log(f"成功抓取 {len(result)} 条推文")
        return result

    except Exception as e:
        log(f"抓取失败: {e}", "ERROR")
        debug(traceback.format_exc())
        return []


def fetch_tweets(screen_name: str, max_count: int = MAX_TWEETS_PER_CHECK) -> list[dict]:
    """同步包装"""
    return asyncio.run(fetch_tweets_async(screen_name, max_count))


# ══════════════════════════════════════════════
#  邮件发送（QQ SMTP）
# ══════════════════════════════════════════════

def send_email(subject: str, html_body: str, to_email: str = "") -> bool:
    recipient = to_email or RECIPIENT_EMAIL
    sender = SENDER_EMAIL
    password = SENDER_PASSWORD

    missing = []
    if not sender:
        missing.append("SENDER_EMAIL")
    if not password:
        missing.append("SENDER_PASSWORD")
    if not recipient:
        missing.append("RECIPIENT_EMAIL")
    if missing:
        log(f"邮件配置缺失: {', '.join(missing)}", "ERROR")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    plain = re.sub(r"<[^>]+>", "", html_body).strip()
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        log(f"连接 {SMTP_SERVER}:{SMTP_PORT} ...")
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
                s.login(sender, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(sender, password)
                s.send_message(msg)

        log(f"邮件发送成功 -> {recipient}")
        return True

    except smtplib.SMTPAuthenticationError:
        log("SMTP 认证失败！请检查授权码", "ERROR")
        return False
    except Exception as e:
        log(f"邮件发送失败: {e}", "ERROR")
        return False


def send_error_notification(error_msg: str) -> None:
    subject = f"[X监控] 脚本异常 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    body = f"""
    <h2>X 推文监控异常</h2>
    <p>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC)</p>
    <pre>{error_msg}</pre>
    """
    send_email(subject, body)


# ══════════════════════════════════════════════
#  去重 & 状态管理
# ══════════════════════════════════════════════

def load_sent_tweets() -> set[str]:
    if SENT_TWEETS_FILE.exists():
        try:
            data = json.loads(SENT_TWEETS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ids = data.get("tweet_ids", [])
            else:
                ids = data
            return set(ids[-500:])
        except (json.JSONDecodeError, TypeError):
            pass
    return set()


def save_sent_tweets(ids: set[str]) -> None:
    data = {
        "tweet_ids": list(ids)[-500:],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    SENT_TWEETS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def deduplicate(tweets: list[dict]) -> list[dict]:
    sent = load_sent_tweets()
    new_list = []
    new_ids = set()
    for t in tweets:
        tid = t.get("id", "")
        if tid and tid not in sent:
            new_list.append(t)
            new_ids.add(tid)
            sent.add(tid)
    if new_ids:
        save_sent_tweets(sent)
        log(f"发现 {len(new_ids)} 条新推文（共 {len(tweets)} 条，过滤 {len(tweets) - len(new_ids)} 条重复）")
    else:
        log(f"无新推文（{len(tweets)} 条已发送过）")
    return new_list


# ══════════════════════════════════════════════
#  邮件模板
# ══════════════════════════════════════════════

def build_email_html(tweets: list[dict], target_user: str) -> str:
    if not tweets:
        return ""

    utc8 = timezone(timedelta(hours=8))
    cards = ""

    for t in tweets:
        text = (t.get("full_text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        text = re.sub(r'(https?://\S+)', r'<a href="\1" style="color:#1d9bf0;">\1</a>', text)

        created_str = t.get("created_at", "")
        time_display = created_str[:19] if created_str else "未知"

        cards += f"""
        <div style="margin-bottom:20px;padding:16px;border:1px solid #e1e8ed;border-radius:12px;background:#fff;">
            <div style="font-size:15px;line-height:1.6;color:#0f1419;margin-bottom:12px;">{text}</div>
            <div style="font-size:13px;color:#536471;margin-bottom:8px;">{time_display}</div>
            <div style="font-size:13px;color:#536471;">
                💬 {t.get('reply_count', 0):,} &nbsp; 🔁 {t.get('retweet_count', 0):,} &nbsp; ❤️ {t.get('favorite_count', 0):,}
            </div>
            <div style="margin-top:12px;">
                <a href="{t.get('url', '#')}" style="display:inline-block;padding:6px 16px;background:#1d9bf0;color:#fff;text-decoration:none;border-radius:20px;font-size:13px;">查看原文</a>
            </div>
        </div>"""

    return f"""
    <html><body style="margin:0;padding:20px;background:#f7f9fa;font-family:sans-serif;">
    <div style="max-width:600px;margin:0 auto;">
        <div style="background:#1d9bf0;padding:24px;border-radius:12px 12px 0 0;color:#fff;">
            <h2 style="margin:0;">X 推文提醒</h2>
            <p style="margin:4px 0 0;">@{target_user} · {len(tweets)} 条新推文</p>
        </div>
        <div style="background:#fff;padding:20px;border:1px solid #e1e8ed;">{cards}</div>
        <div style="background:#f7f9fa;padding:12px;border:1px solid #e1e8ed;border-radius:0 0 12px 12px;text-align:center;font-size:11px;color:#8899a6;">
            X Monitor Bot · GitHub Actions · 免费运行
        </div>
    </div></body></html>"""


# ══════════════════════════════════════════════
#  主逻辑
# ══════════════════════════════════════════════

def check_and_notify() -> dict:
    summary = {
        "target": TARGET_SCREEN_NAME,
        "fetched": 0,
        "new": 0,
        "email_sent": False,
        "error": None,
    }

    # 抓取
    tweets = fetch_tweets(TARGET_SCREEN_NAME)
    summary["fetched"] = len(tweets)

    if tweets:
        tweets.sort(key=lambda t: t.get("id", ""), reverse=True)

    # 去重
    new_tweets = deduplicate(tweets)
    summary["new"] = len(new_tweets)

    # 发邮件
    if new_tweets:
        html = build_email_html(new_tweets, TARGET_SCREEN_NAME)
        subject = f"[X监控] @{TARGET_SCREEN_NAME} 发布了 {len(new_tweets)} 条新推文"
        if send_email(subject, html):
            summary["email_sent"] = True
            log(f"完成: {len(new_tweets)} 条新推文已发送")
        else:
            summary["error"] = "邮件发送失败"
    else:
        log("无新推文，跳过邮件")

    return summary


# ══════════════════════════════════════════════
#  Cookies 获取向导
# ══════════════════════════════════════════════

def interactive_login_guide():
    print("\n" + "=" * 60)
    print("  X (Twitter) Cookies 获取向导")
    print("=" * 60)
    print("""
1. 浏览器打开 https://x.com 并登录
2. 用 Cookie-Editor 插件导出 cookies JSON
3. 粘贴到下方
""")
    print("-" * 60)
    print("粘贴 cookies JSON（Ctrl+Z 然后回车结束）:")
    lines = []
    try:
        while True:
            lines.append(input())
    except (EOFError, KeyboardInterrupt):
        pass

    raw = "\n".join(lines)
    try:
        cookies = json.loads(raw)
        if isinstance(cookies, dict):
            cookies = [cookies]
        COOKIES_FILE.write_text(
            json.dumps(cookies, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"已保存 {len(cookies)} 个 cookies 到 {COOKIES_FILE}")
    except json.JSONDecodeError as e:
        print(f"JSON 格式错误: {e}")


# ══════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="X 推文监控 + 邮件转发")
    parser.add_argument("--login", action="store_true", help="交互式获取 cookies")
    parser.add_argument("--test-email", action="store_true", help="测试邮件发送")
    parser.add_argument("--startup", action="store_true", help="发送启动通知")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--target", type=str, default="", help="临时目标用户")

    args = parser.parse_args()

    global VERBOSE, TARGET_SCREEN_NAME
    if args.verbose:
        VERBOSE = True
    if args.target:
        TARGET_SCREEN_NAME = args.target

    if args.login:
        interactive_login_guide()
        return

    if args.test_email:
        print("\n发送测试邮件...")
        html = f"<h2>测试邮件</h2><p>SMTP: {SMTP_SERVER}:{SMTP_PORT}</p><p>发件: {SENDER_EMAIL}</p><p>收件: {RECIPIENT_EMAIL}</p><p>目标: @{TARGET_SCREEN_NAME}</p>"
        ok = send_email("[X监控] 测试邮件", html)
        print("成功!" if ok else "失败!")
        return

    if args.startup:
        send_email(
            f"[X监控] 监控已启动 - @{TARGET_SCREEN_NAME}",
            f"<h2>监控已启动</h2><p>目标: @{TARGET_SCREEN_NAME}</p><p>发到: {RECIPIENT_EMAIL}</p>",
        )
        return

    print("\n" + "=" * 50)
    print(f"  X 推文监控")
    print(f"  目标: @{TARGET_SCREEN_NAME}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50 + "\n")

    try:
        summary = check_and_notify()
        print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log(err, "ERROR")
        try:
            send_error_notification(err)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

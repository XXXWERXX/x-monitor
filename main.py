#!/usr/bin/env python3
"""
X (Twitter) 推文监控 + 邮件转发
========================================
通过 X 公开 GraphQL API 抓取推文，QQ 邮箱转发。
适配 2026 年 6 月 X 前端。

原理: GitHub Actions 运行 → 获取 X guest token → 调用公开 API → 邮件通知
"""

import argparse
import json
import os
import random
import re
import smtplib
import string
import sys
import time
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
#  HTTP 请求基础
# ══════════════════════════════════════════════

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def _base_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://x.com",
        "Referer": "https://x.com/",
        "Sec-Fetch-Site": "same-origin",
    }


def _proxy() -> Optional[dict]:
    p = HTTPS_PROXY or HTTP_PROXY or None
    if p:
        return {"https": p, "http": p}
    return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_base_headers())
    if _proxy():
        s.proxies.update(_proxy())
    return s


def _random_csrf() -> str:
    """生成随机 32 位 hex csrf token（X 接受任意 32 位 hex）"""
    return "".join(random.choices("0123456789abcdef", k=32))


# ══════════════════════════════════════════════
#  X 公开 API 抓取
# ══════════════════════════════════════════════

def _get_guest_token(session: requests.Session) -> Optional[str]:
    """获取 X guest token（公开 API，无需登录）"""
    try:
        # X 的公开激活接口
        headers = {
            "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = session.post(
            "https://api.x.com/1.1/guest/activate.json",
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            token = resp.json().get("guest_token", "")
            if token:
                log(f"获取 guest token: {token[:10]}...")
                return token
        log(f"获取 guest token 失败 (HTTP {resp.status_code})", "WARN")
    except Exception as e:
        log(f"获取 guest token 异常: {e}", "WARN")
    return None


def _load_cookies_to_session(session: requests.Session) -> Optional[str]:
    """加载 cookies 到 session，返回 auth_token"""
    cookie_list = None

    env_cookies = os.getenv("X_COOKIES", "")
    if env_cookies:
        try:
            cookie_list = json.loads(env_cookies)
        except json.JSONDecodeError:
            pass

    if cookie_list is None and COOKIES_FILE.exists():
        try:
            cookie_list = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass

    if not cookie_list:
        return None

    auth_token = None
    for c in cookie_list:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
        if c["name"] == "auth_token":
            auth_token = c["value"]

    log(f"加载 {len(cookie_list)} 个 cookies" + (", 含 auth_token" if auth_token else ""))
    return auth_token


# ── 动态获取 query ID ──

def _extract_query_ids(session: requests.Session) -> dict:
    """
    从 X 首页的 JS 源码中动态提取 GraphQL query ID。
    这样每次运行都自动适配最新接口。
    """
    try:
        # 获取 X 首页 HTML
        resp = session.get("https://x.com/", timeout=15)
        html = resp.text

        # 找主 JS bundle URL: main.<hash>.js
        js_urls = re.findall(
            r'src="(https://abs\.twimg\.com/responsive-web/client-web/[^"]+\.js)"',
            html,
        )
        if not js_urls:
            js_urls = re.findall(
                r'(https://abs\.twimg\.com/responsive-web/client-web/main\.[a-f0-9]+\.js)',
                html,
            )
        if not js_urls:
            # 备用: 匹配任何 abs.twimg.com JS
            js_urls = re.findall(
                r'(https://abs\.twimg\.com/responsive-web/[^"]+\.js)',
                html,
            )

        if not js_urls:
            log("未找到 X JS bundle URL", "WARN")
            return _fallback_query_ids()

        # 下载 JS bundle
        js_url = js_urls[0]
        debug(f"JS bundle: {js_url}")
        js_resp = session.get(js_url, timeout=15)
        js_text = js_resp.text

        # 提取 query IDs: "queryId":"<id>","operationName":"UserByScreenName"
        query_ids = {}

        patterns = {
            "UserByScreenName": r'"queryId":"([a-zA-Z0-9_\-]+)"[^}]*"operationName":"UserByScreenName"',
            "UserTweets": r'"queryId":"([a-zA-Z0-9_\-]+)"[^}]*"operationName":"UserTweets"',
        }

        for name, pattern in patterns.items():
            m = re.search(pattern, js_text)
            if m:
                query_ids[name] = m.group(1)
                debug(f"找到 {name}: {m.group(1)}")

        if query_ids:
            log(f"动态提取 query IDs: {query_ids}")
            return query_ids

    except Exception as e:
        log(f"提取 query IDs 失败: {e}", "WARN")

    return _fallback_query_ids()


def _fallback_query_ids() -> dict:
    """当动态提取失败时的硬编码备选"""
    return {
        "UserByScreenName": "32pL5BWe9WKeSK1MoPvFQQ",
        "UserTweets": "Y9WM4Id6UcGFE8Z-hbnixw",
    }


# ── API 调用 ──

def _make_graphql_request(
    session: requests.Session,
    query_id: str,
    operation_name: str,
    variables: dict,
    guest_token: str = "",
    csrf_token: str = "",
) -> Optional[dict]:
    """调用 X GraphQL API"""
    url = f"https://x.com/i/api/graphql/{query_id}/{operation_name}"
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps({
            "responsive_web_graphql_exclude_directive_enabled": True,
            "verified_phone_label_enabled": False,
            "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
            "responsive_web_enhance_cards_enabled": False,
        }, separators=(",", ":")),
    }

    headers = {
        "Content-Type": "application/json",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }

    if guest_token:
        headers["x-guest-token"] = guest_token
    if csrf_token:
        headers["x-csrf-token"] = csrf_token
        # 添加随机 CSRF cookie（X 要求的）
        if "ct0" not in session.cookies:
            session.cookies.set("ct0", csrf_token, domain=".x.com")

    try:
        resp = session.get(url, params=params, headers=headers, timeout=20)
        debug(f"{operation_name} HTTP {resp.status_code}")

        if resp.status_code == 429:
            log(f"速率限制 ({operation_name})，等待 60 秒...", "WARN")
            time.sleep(60)
            return None
        if resp.status_code == 403:
            log(f"HTTP 403 ({operation_name})", "WARN")
            return None
        if resp.status_code != 200:
            log(f"HTTP {resp.status_code} ({operation_name}): {resp.text[:300]}", "ERROR")
            return None

        return resp.json()

    except Exception as e:
        log(f"API 请求失败 ({operation_name}): {e}", "ERROR")
        return None


def _get_user_id(session: requests.Session, screen_name: str,
                 query_ids: dict, guest_token: str, csrf_token: str) -> Optional[str]:
    """通过用户名获取 user ID"""
    data = _make_graphql_request(
        session,
        query_ids["UserByScreenName"],
        "UserByScreenName",
        {"screen_name": screen_name, "withSafetyModeUserFields": True},
        guest_token,
        csrf_token,
    )
    if not data:
        # 尝试备用 ID
        alt_id = "u7wQyGi6oExe8_TRWGMq4Q"
        data = _make_graphql_request(
            session,
            alt_id,
            "UserByScreenName",
            {"screen_name": screen_name, "withSafetyModeUserFields": True},
            guest_token,
            csrf_token,
        )

    if data:
        try:
            user_result = data.get("data", {}).get("user", {}).get("result", {})
            rest_id = user_result.get("rest_id", "")
            if rest_id:
                log(f"用户 @{screen_name} ID: {rest_id}")
                return rest_id
        except Exception:
            pass
    return None


def _parse_tweets(data: dict, screen_name: str) -> list[dict]:
    """解析推文响应"""
    tweets = []
    try:
        result = data.get("data", {}).get("user", {}).get("result", {})
        timeline = result.get("timeline_v2", {}).get("timeline", {})
        instructions = timeline.get("instructions", [])

        for instr in instructions:
            if instr.get("type") not in ("TimelineAddEntries",):
                continue
            for entry in instr.get("entries", []):
                eid = entry.get("entryId", "")
                if eid.startswith(("cursor-", "who-to-follow", "user-", "prompt-")):
                    continue

                content = entry.get("content", {})
                item = content.get("itemContent", {}) or content.get("items", [{}])
                if isinstance(item, list):
                    if not item:
                        continue
                    item = item[0].get("item", {}).get("itemContent", {})

                tweet_res = item.get("tweet_results", {}).get("result", {})
                if tweet_res.get("__typename") == "TweetWithVisibilityResults":
                    tweet_res = tweet_res.get("tweet", {})

                legacy = tweet_res.get("legacy", {})
                if not legacy:
                    continue

                core = tweet_res.get("core", {})
                user_legacy = (
                    core.get("user_results", {}).get("result", {}).get("legacy", {})
                )

                tid = legacy.get("id_str", "") or tweet_res.get("rest_id", "")
                tweets.append({
                    "id": tid,
                    "created_at": legacy.get("created_at", ""),
                    "full_text": legacy.get("full_text", ""),
                    "favorite_count": legacy.get("favorite_count", 0),
                    "retweet_count": legacy.get("retweet_count", 0),
                    "reply_count": legacy.get("reply_count", 0),
                    "quote_count": legacy.get("quote_count", 0),
                    "view_count": legacy.get("views", {}).get("count", "N/A"),
                    "author_name": user_legacy.get("name", screen_name),
                    "author_screen_name": user_legacy.get("screen_name", screen_name),
                    "url": f"https://x.com/{user_legacy.get('screen_name', screen_name)}/status/{tid}",
                })

    except Exception as e:
        log(f"解析推文出错: {e}", "ERROR")
        debug(traceback.format_exc())

    return tweets


def fetch_tweets(screen_name: str, max_count: int = MAX_TWEETS) -> list[dict]:
    """主抓取流程"""
    log(f"开始抓取 @{screen_name} ...")

    session = _session()

    # 1. 动态提取 query IDs
    query_ids = _extract_query_ids(session)

    # 2. 加载 cookies / 获取 guest token
    csrf_token = ""
    guest_token = ""
    auth_token = _load_cookies_to_session(session)

    if auth_token:
        # 有 cookies: 用 auth_token 中的 ct0 做 CSRF
        for cookie in session.cookies:
            if cookie.name == "ct0":
                csrf_token = cookie.value
                break
        # 如果 cookies 里没有 ct0，先访问首页获取
        if not csrf_token:
            try:
                session.get("https://x.com/", timeout=15)
                for cookie in session.cookies:
                    if cookie.name == "ct0":
                        csrf_token = cookie.value
                        break
            except Exception:
                pass
        if not csrf_token:
            csrf_token = _random_csrf()
            session.cookies.set("ct0", csrf_token, domain=".x.com")
        log("使用已登录 cookies 模式")
    else:
        # 无 cookies: 用 guest token
        csrf_token = _random_csrf()
        guest_token = _get_guest_token(session) or ""
        session.cookies.set("ct0", csrf_token, domain=".x.com")
        log("使用访客模式")

    # 3. 获取用户 ID
    user_id = _get_user_id(session, screen_name, query_ids,
                           guest_token or "", csrf_token)
    if not user_id:
        log("无法获取用户 ID", "ERROR")
        session.close()
        return []

    # 4. 获取推文
    data = _make_graphql_request(
        session,
        query_ids.get("UserTweets", _fallback_query_ids()["UserTweets"]),
        "UserTweets",
        {
            "userId": user_id,
            "count": max_count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        },
        guest_token or "",
        csrf_token,
    )

    # 如果主 query ID 失败，尝试备用
    if not data:
        alt_id = "JLApJKFY0MxGTzCoK6ps8Q"
        data = _make_graphql_request(
            session, alt_id, "UserTweets",
            {
                "userId": user_id, "count": max_count,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True, "withV2Timeline": True,
            },
            guest_token or "", csrf_token,
        )

    session.close()

    if not data:
        log("无法获取推文数据", "ERROR")
        return []

    tweets = _parse_tweets(data, screen_name)
    log(f"抓取到 {len(tweets)} 条推文")
    return tweets


# ══════════════════════════════════════════════
#  邮件发送
# ══════════════════════════════════════════════

def send_email(subject: str, html_body: str, to_email: str = "") -> bool:
    recipient = to_email or RECIPIENT_EMAIL
    sender = SENDER_EMAIL
    password = SENDER_PASSWORD

    if not sender or not password or not recipient:
        log("邮件配置缺失 (SENDER_EMAIL/SENDER_PASSWORD/RECIPIENT_EMAIL)", "ERROR")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    plain = re.sub(r"<[^>]+>", "", html_body).strip()
    msg.attach(MIMEText(plain, "plain", "utf-8"))
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
        log(f"邮件发送成功 -> {recipient}")
        return True
    except Exception as e:
        log(f"邮件发送失败: {e}", "ERROR")
        return False


def send_error_notification(err: str) -> None:
    send_email(
        f"[X监控] 脚本异常 - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"<h2>异常</h2><p>{datetime.now()}</p><pre>{err[:2000]}</pre>",
    )


# ══════════════════════════════════════════════
#  去重
# ══════════════════════════════════════════════

def load_sent() -> set[str]:
    if SENT_TWEETS_FILE.exists():
        try:
            data = json.loads(SENT_TWEETS_FILE.read_text(encoding="utf-8"))
            ids = data.get("tweet_ids", []) if isinstance(data, dict) else data
            return set(ids[-500:])
        except Exception:
            pass
    return set()


def save_sent(ids: set[str]) -> None:
    SENT_TWEETS_FILE.write_text(
        json.dumps({"tweet_ids": list(ids)[-500:], "last_updated": datetime.now(timezone.utc).isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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
        log(f"{len(new_list)} 条新推文 / {len(tweets)} 条总量")
    else:
        log("无新推文")
    return new_list


# ══════════════════════════════════════════════
#  邮件模板
# ══════════════════════════════════════════════

def build_email(tweets: list[dict], target: str) -> str:
    cards = ""
    for t in tweets:
        text = (t.get("full_text") or "").replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        text = re.sub(r'(https?://\S+)', r'<a href="\1" style="color:#1d9bf0;">\1</a>', text)
        cards += f"""
        <div style="margin-bottom:16px;padding:16px;border:1px solid #e1e8ed;border-radius:12px;background:#fff;">
            <div style="font-size:15px;line-height:1.6;color:#0f1419;margin-bottom:8px;">{text}</div>
            <div style="font-size:12px;color:#536471;">{t.get('created_at', '')[:19]}</div>
            <div style="font-size:12px;color:#536471;margin-top:4px;">
                💬{t.get('reply_count',0):,} 🔁{t.get('retweet_count',0):,} ❤️{t.get('favorite_count',0):,}
            </div>
            <div style="margin-top:8px;">
                <a href="{t.get('url','#')}" style="display:inline-block;padding:6px 14px;background:#1d9bf0;color:#fff;text-decoration:none;border-radius:20px;font-size:12px;">查看原文</a>
            </div>
        </div>"""

    return f"""
    <html><body style="margin:0;padding:20px;background:#f7f9fa;font-family:-apple-system,sans-serif;">
    <div style="max-width:600px;margin:0 auto;">
        <div style="background:#1d9bf0;padding:20px 24px;border-radius:12px 12px 0 0;color:#fff;">
            <h2 style="margin:0;">X 推文提醒</h2>
            <p style="margin:4px 0 0;font-size:14px;">@{target} · {len(tweets)} 条新推文</p>
        </div>
        <div style="background:#fff;padding:20px;border:1px solid #e1e8ed;">{cards}</div>
        <div style="background:#f7f9fa;padding:12px;text-align:center;font-size:11px;color:#8899a6;border:1px solid #e1e8ed;border-radius:0 0 12px 12px;">
            X Monitor Bot · GitHub Actions · 免费运行
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
    print("1. 浏览器登录 https://x.com\n2. Cookie-Editor 插件导出 JSON\n3. 粘贴到下方\n")
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
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已保存 {len(cookies)} 个 cookies")
    except json.JSONDecodeError as e:
        print(f"JSON 错误: {e}")


def main():
    parser = argparse.ArgumentParser(description="X 推文监控 + 邮件转发")
    parser.add_argument("--login", action="store_true", help="获取 cookies")
    parser.add_argument("--test-email", action="store_true", help="测试邮件")
    parser.add_argument("--startup", action="store_true", help="启动通知")
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
        ok = send_email("[X监控] 测试", f"<h2>测试</h2><p>目标: @{TARGET_SCREEN_NAME}</p><p>{datetime.now()}</p>")
        print("成功!" if ok else "失败!")
        return
    if args.startup:
        send_email(f"[X监控] 启动 - @{TARGET_SCREEN_NAME}", f"<h2>监控已启动</h2><p>@{TARGET_SCREEN_NAME}</p>")
        return

    print(f"\n{'='*50}\n  X 推文监控\n  目标: @{TARGET_SCREEN_NAME}\n{'='*50}\n")
    try:
        r = check_and_notify()
        print("\n" + json.dumps(r, ensure_ascii=False, indent=2))
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

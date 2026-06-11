#!/usr/bin/env python3
"""
X (Twitter) 推文监控 + Outlook 邮件转发
========================================
定时抓取指定博主的推文，检测到新推文时通过 Outlook SMTP 发送邮件。
设计为在 GitHub Actions 上运行（境外 IP，无需 VPN），零成本。

使用方式:
  1. 本地首次运行获取 cookies:  python main.py --login
  2. GitHub Actions 定时运行:     python main.py
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

# 修复 Windows 终端 GBK 编码无法输出 emoji 的问题
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 加载 .env 文件（本地开发用，无需额外依赖） ──
def _load_dotenv():
    """从 .env 文件加载环境变量（简单实现，无需 python-dotenv）"""
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

import httpx

# ──────────────────────────────────────────────
#  配置区（可通过环境变量覆盖）
# ──────────────────────────────────────────────

# ---- X / Twitter 配置 ----
TARGET_SCREEN_NAME = os.getenv("TARGET_SCREEN_NAME", "elonmusk")  # 目标博主用户名
MAX_TWEETS_PER_CHECK = int(os.getenv("MAX_TWEETS_PER_CHECK", "10"))  # 每次最多抓几条

# ---- 邮件配置（QQ 邮箱） ----
# QQ 邮箱授权码获取: 登录 QQ 邮箱 → 设置 → 账户 → POP3/SMTP 服务 → 开启 → 生成授权码
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))          # QQ 邮箱 SSL 端口
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")       # 你的 QQ 邮箱（在 .env 文件中设置）
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD", "")  # QQ 邮箱授权码（在 .env 文件中设置）
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL", "")  # 接收邮件的邮箱（在 .env 文件中设置）

# ---- 运行模式 ----
DATA_DIR = Path(os.getenv("DATA_DIR", str(Path(__file__).parent)))
STATE_FILE = DATA_DIR / "last_tweets.json"            # 记录已发送的推文 ID
COOKIES_FILE = DATA_DIR / "x_cookies.json"            # X 登录 cookies
SENT_TWEETS_FILE = DATA_DIR / "sent_tweets.json"      # 已发送推文记录（用于去重）

# ---- 代理（本地测试用，GitHub Actions 不需要） ----
HTTP_PROXY = os.getenv("HTTP_PROXY", "")   # 如 http://127.0.0.1:7890
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

# ---- 日志 ----
VERBOSE = os.getenv("VERBOSE", "0") == "1"

# ──────────────────────────────────────────────
#  X GraphQL API 常量（2025-2026 可用）
#  如果 X 前端更新，这些 query_id 可能需要更换
#  获取最新 ID 的方法：打开 X.com → F12 → 搜索 "UserTweets" → 复制 queryId
# ──────────────────────────────────────────────

# 获取用户信息的 GraphQL query ID
USER_BY_SCREEN_NAME_QUERY_ID = "qRednkZG1C17S1bRaVkaOA"
# 获取用户推文的 GraphQL query ID (UserTweets)
USER_TWEETS_QUERY_ID = "V7H0jFSMyp4gzwGJhLG75g"

# 这些是 X 网页版的标准 feature switches，需要和浏览器一致
X_FEATURES = {
    "articles_preview_enabled": True,
    "blue_business_profile_image_shape_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translation_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": False,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_text_conversations_enabled": False,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "responsive_web_twitter_blue_verified_badge_is_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
}


def log(msg: str, level: str = "INFO") -> None:
    """统一日志输出"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr)


def debug(msg: str) -> None:
    if VERBOSE:
        log(msg, "DEBUG")


# ════════════════════════════════════════════════
#  X / Twitter 爬虫模块
# ════════════════════════════════════════════════

def _build_client() -> httpx.Client:
    """构建带浏览器伪装头的 httpx 客户端"""
    proxy = HTTPS_PROXY or HTTP_PROXY or None
    return httpx.Client(
        timeout=30,
        follow_redirects=True,
        proxy=proxy,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://x.com/",
            "Origin": "https://x.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "zh-cn",
        },
    )


def _load_cookies(client: httpx.Client) -> bool:
    """
    从文件和环境变量加载 cookies。
    GitHub Actions 下通过 X_COOKIES 环境变量注入；
    本地运行通过 COOKIES_FILE 文件加载。
    返回 True 表示成功加载。
    """
    # 方式1：环境变量（GitHub Secrets，优先级高）
    env_cookies = os.getenv("X_COOKIES", "")
    if env_cookies:
        try:
            cookie_list = json.loads(env_cookies)
            for c in cookie_list:
                client.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
            log(f"从环境变量加载了 {len(cookie_list)} 个 cookies")
            return True
        except json.JSONDecodeError:
            log("X_COOKIES 环境变量 JSON 格式错误，尝试文件加载", "WARN")

    # 方式2：本地 cookies 文件
    if COOKIES_FILE.exists():
        try:
            cookie_list = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            for c in cookie_list:
                client.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
            log(f"从文件加载了 {len(cookie_list)} 个 cookies: {COOKIES_FILE}")
            return True
        except (json.JSONDecodeError, KeyError):
            log(f"Cookies 文件格式错误: {COOKIES_FILE}", "ERROR")
            return False

    log("未找到 cookies（环境变量 X_COOKIES 和文件 x_cookies.json 均不存在）", "WARN")
    return False


def _parse_tweets_from_api(response_data: dict) -> list[dict]:
    """
    从 X API 响应中解析推文。
    处理 UserTweets GraphQL 的响应结构。
    """
    tweets = []

    try:
        # UserTweets 的响应结构:
        # data.user.result.timeline_v2.timeline.instructions[].entries[]
        user_result = response_data.get("data", {}).get("user", {}).get("result", {})
        timeline = user_result.get("timeline_v2", {}).get("timeline", {})
        instructions = timeline.get("instructions", [])

        if not instructions:
            # 另一种路径：data.user.result.timeline_response.timeline.instructions
            timeline_resp = user_result.get("timeline_response", {}).get("timeline", {})
            instructions = timeline_resp.get("instructions", [])

        for instruction in instructions:
            if instruction.get("type") not in ("TimelineAddEntries", "TimelineAddToModule"):
                continue
            for entry in instruction.get("entries", []):
                entry_id = entry.get("entryId", "")
                # 跳过非推文条目（如 cursor 分页游标、who-to-follow 推荐）
                if entry_id.startswith("cursor-") or entry_id.startswith("who-to-follow"):
                    continue
                if entry_id.startswith("user-") or entry_id.startswith("prompt-"):
                    continue

                content = entry.get("content", {})
                item_content = (
                    content.get("itemContent", {}) or
                    content.get("items", [{}])  # 列表中的第一个通常是 tweet
                )
                if isinstance(item_content, list):
                    if not item_content:
                        continue
                    item_content = item_content[0].get("item", {}).get("itemContent", {})

                tweet_result = item_content.get("tweet_results", {}).get("result", {})
                if not tweet_result:
                    continue

                # 处理可能的 __typename 包装
                if tweet_result.get("__typename") == "TweetWithVisibilityResults":
                    tweet_result = tweet_result.get("tweet", {})

                legacy = tweet_result.get("legacy", {})
                if not legacy:
                    # 可能是 retweeted_status 之类
                    tweet_result = tweet_result.get("retweeted_status_result", {}).get("result", {})
                    legacy = tweet_result.get("legacy", {})

                if not legacy:
                    continue

                core = tweet_result.get("core", {})
                user_info = (
                    core.get("user_results", {})
                    .get("result", {})
                    .get("legacy", {})
                )

                # 提取推文信息
                tweet = {
                    "id": legacy.get("id_str", ""),
                    "rest_id": tweet_result.get("rest_id", ""),
                    "created_at": legacy.get("created_at", ""),
                    "full_text": legacy.get("full_text", ""),
                    "lang": legacy.get("lang", ""),
                    "favorite_count": legacy.get("favorite_count", 0),
                    "retweet_count": legacy.get("retweet_count", 0),
                    "reply_count": legacy.get("reply_count", 0),
                    "quote_count": legacy.get("quote_count", 0),
                    "view_count": legacy.get("views", {}).get("count", "N/A"),
                    "author_name": user_info.get("name", "Unknown"),
                    "author_screen_name": user_info.get("screen_name", "Unknown"),
                    "url": (
                        f"https://x.com/{user_info.get('screen_name', '')}"
                        f"/status/{legacy.get('id_str', '')}"
                    ),
                }
                if tweet["id"]:
                    tweets.append(tweet)

    except Exception as e:
        log(f"解析 API 响应时出错: {e}", "ERROR")
        debug(traceback.format_exc())

    return tweets


def _get_user_id(client: httpx.Client, screen_name: str) -> Optional[str]:
    """通过用户名获取 X 用户 ID（rest_id）"""
    variables = json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True})
    features = json.dumps(X_FEATURES)
    params = {"variables": variables, "features": features}

    try:
        resp = client.get(
            "https://x.com/i/api/graphql/"
            f"{USER_BY_SCREEN_NAME_QUERY_ID}/UserByScreenName",
            params=params,
        )
        debug(f"UserByScreenName 响应状态: {resp.status_code}")
        if resp.status_code != 200:
            log(f"获取用户信息失败 (HTTP {resp.status_code}): {resp.text[:500]}", "ERROR")
            return None

        data = resp.json()
        user_result = data.get("data", {}).get("user", {}).get("result", {})
        rest_id = user_result.get("rest_id", "")
        if rest_id:
            log(f"用户 {screen_name} 的 ID: {rest_id}")
            return rest_id

        log(f"未找到用户: {screen_name}", "ERROR")
        return None

    except Exception as e:
        log(f"获取用户 ID 时出错: {e}", "ERROR")
        return None


def fetch_tweets(
    screen_name: str,
    max_count: int = MAX_TWEETS_PER_CHECK,
) -> list[dict]:
    """
    抓取指定用户的最新推文。
    返回推文列表，按时间倒序排列。
    """
    log(f"开始抓取 @{screen_name} 的最新推文...")

    with _build_client() as client:
        # 1. 加载 cookies
        has_cookies = _load_cookies(client)

        # 2. 先访问首页获取 guest token（如果没有 cookies）
        if not has_cookies:
            log("尝试以访客模式访问...")
            try:
                # 获取 guest_id cookie（X 会为访客自动设置）
                client.get("https://x.com/", follow_redirects=True)
                debug(f"Guest cookies: {dict(client.cookies)}")
            except Exception as e:
                log(f"访问 X 首页失败: {e}", "ERROR")

        # 3. 获取用户 ID
        user_id = _get_user_id(client, screen_name)
        if not user_id:
            log("无法获取用户 ID，请检查用户名或 cookies 是否有效", "ERROR")
            return []

        # 4. 获取推文
        variables = json.dumps({
            "userId": user_id,
            "count": max_count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        })
        features = json.dumps(X_FEATURES)
        field_toggles = json.dumps({"withArticlePlainText": False})

        try:
            resp = client.get(
                "https://x.com/i/api/graphql/"
                f"{USER_TWEETS_QUERY_ID}/UserTweets",
                params={
                    "variables": variables,
                    "features": features,
                    "fieldToggles": field_toggles,
                },
            )
            debug(f"UserTweets 响应状态: {resp.status_code}")

            if resp.status_code == 403:
                log("HTTP 403: cookies 可能已过期，需要重新登录获取", "ERROR")
                return []
            if resp.status_code == 429:
                log("HTTP 429: 请求频率限制，等待后重试...", "WARN")
                time.sleep(60)
                return []
            if resp.status_code != 200:
                log(f"获取推文失败 (HTTP {resp.status_code}): {resp.text[:500]}", "ERROR")
                return []

            data = resp.json()
            tweets = _parse_tweets_from_api(data)
            log(f"成功抓取 {len(tweets)} 条推文")
            return tweets

        except httpx.RequestError as e:
            log(f"网络请求失败: {e}", "ERROR")
            return []
        except Exception as e:
            log(f"抓取推文时出错: {e}", "ERROR")
            debug(traceback.format_exc())
            return []


def fetch_tweets_guest_fallback(screen_name: str, max_count: int = 10) -> list[dict]:
    """
    备选方案：使用 nitter.net RSS 或其他公开源（当 cookies 方案失效时）。
    注意：nitter 实例可能不稳定，这是应急方案。
    """
    log("尝试 RSS 备选方案...")
    # 使用多个可能的 nitter 实例
    nitter_instances = [
        "https://nitter.poast.org",
        "https://nitter.privacydev.net",
    ]

    for instance in nitter_instances:
        try:
            with _build_client() as client:
                resp = client.get(f"{instance}/{screen_name}/rss", timeout=20)
                if resp.status_code == 200:
                    log(f"成功从 {instance} 获取 RSS")
                    # 简单的 RSS 解析（不引入额外依赖）
                    tweets = _parse_rss(resp.text)
                    log(f"RSS 方式获取到 {len(tweets)} 条推文")
                    return tweets[:max_count]
        except Exception:
            continue

    log("所有 RSS 备选方案均失败", "ERROR")
    return []


def _parse_rss(rss_text: str) -> list[dict]:
    """从 RSS XML 中解析推文（简易解析器，避免依赖 lxml）"""
    tweets = []
    # 匹配 <item>...</item> 区块
    item_pattern = re.compile(r"<item>(.*?)</item>", re.DOTALL)
    for item_match in item_pattern.finditer(rss_text):
        item = item_match.group(1)
        title = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
        link = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
        pub_date = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
        guid = re.search(r"<guid[^>]*>(.*?)</guid>", item, re.DOTALL)

        if title and link:
            tweet_id = ""
            if guid:
                # GUID 通常是 URL，从中提取推文 ID
                id_match = re.search(r"/status/(\d+)", guid.group(1))
                if id_match:
                    tweet_id = id_match.group(1)
            if not tweet_id and link:
                id_match = re.search(r"/status/(\d+)", link.group(1))
                if id_match:
                    tweet_id = id_match.group(1)

            tweets.append({
                "id": tweet_id,
                "rest_id": tweet_id,
                "created_at": pub_date.group(1) if pub_date else "",
                "full_text": title.group(1),
                "lang": "",
                "favorite_count": 0,
                "retweet_count": 0,
                "reply_count": 0,
                "quote_count": 0,
                "view_count": "N/A",
                "author_name": screen_name_from_rss(rss_text),
                "author_screen_name": screen_name_from_rss(rss_text),
                "url": link.group(1),
            })

    return tweets


def screen_name_from_rss(rss_text: str) -> str:
    """从 RSS 中提取用户名"""
    m = re.search(r"<title>(.*?)/rss</title>", rss_text)
    if m:
        return m.group(1).split("/")[0].strip()
    return "Unknown"


# ════════════════════════════════════════════════
#  邮件发送模块（Outlook SMTP）
# ════════════════════════════════════════════════

def send_email(
    subject: str,
    html_body: str,
    to_email: str = "",
    cc_emails: Optional[list[str]] = None,
) -> bool:
    """
    通过 QQ 邮箱 SMTP 发送邮件（也兼容 Outlook 等其他邮箱）。

    QQ 邮箱前置条件:
      - 登录 QQ 邮箱网页版 → 设置 → 账户
      - 找到 POP3/IMAP/SMTP 服务 → 开启 SMTP 服务
      - 生成"授权码"（不是 QQ 密码！不是 QQ 密码！）

    参数:
      subject:   邮件标题
      html_body: HTML 格式的邮件正文
      to_email:  收件人（为空则用环境变量 RECIPIENT_EMAIL）
    """
    recipient = to_email or RECIPIENT_EMAIL
    sender = SENDER_EMAIL
    password = SENDER_PASSWORD
    cc_list = cc_emails or []

    # ── 参数校验 ──
    missing = []
    if not sender:
        missing.append("SENDER_EMAIL")
    if not password:
        missing.append("SENDER_PASSWORD")
    if not recipient:
        missing.append("RECIPIENT_EMAIL")

    if missing:
        log(f"邮件配置缺失: {', '.join(missing)}", "ERROR")
        log("请在环境变量中设置这些值", "ERROR")
        return False

    # ── 构建邮件 ──
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    # 纯文本备用
    plain_body = re.sub(r"<[^>]+>", "", html_body).strip()
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # ── 发送 ──
    # QQ 邮箱用 465 端口 (SSL)，Outlook 用 587 端口 (STARTTLS)
    try:
        log(f"正在连接 {SMTP_SERVER}:{SMTP_PORT} ...")

        if SMTP_PORT == 465:
            # SSL 直连模式（QQ 邮箱）
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.login(sender, password)
                server.send_message(msg)
        else:
            # STARTTLS 模式（Outlook 等其他邮箱）
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(sender, password)
                server.send_message(msg)

        log(f"邮件发送成功 → {recipient}")
        return True

    except smtplib.SMTPAuthenticationError:
        log("SMTP 认证失败！", "ERROR")
        log("请检查授权码是否正确（QQ邮箱用授权码，不是QQ密码！）", "ERROR")
        return False
    except smtplib.SMTPConnectError:
        log(f"无法连接到 {SMTP_SERVER}:{SMTP_PORT}", "ERROR")
        return False
    except smtplib.SMTPException as e:
        log(f"SMTP 错误: {e}", "ERROR")
        return False
    except Exception as e:
        log(f"发送邮件时发生未知错误: {e}", "ERROR")
        debug(traceback.format_exc())
        return False


def send_error_notification(error_msg: str) -> None:
    """发送程序运行异常的通知邮件"""
    subject = f"[X监控] ⚠️ 脚本运行异常 - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    body = f"""
    <html><body>
    <h2>X 推文监控脚本异常</h2>
    <p><strong>时间:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC)</p>
    <pre style="background:#f5f5f5;padding:15px;border-radius:5px;overflow-x:auto;">{error_msg}</pre>
    <hr>
    <p style="color:#666;font-size:12px;">
        来自 X Monitor Bot · 运行在 GitHub Actions 上
    </p>
    </body></html>
    """
    send_email(subject, body)


def send_startup_notification() -> bool:
    """发送启动通知"""
    subject = f"[X监控] ✅ 监控已启动 - @{TARGET_SCREEN_NAME}"
    body = f"""
    <html><body>
    <h2>✅ X 推文监控已启动</h2>
    <p><strong>监控目标:</strong> <a href="https://x.com/{TARGET_SCREEN_NAME}">@{TARGET_SCREEN_NAME}</a></p>
    <p><strong>启动时间:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (UTC)</p>
    <p><strong>发送邮箱:</strong> {RECIPIENT_EMAIL}</p>
    <hr>
    <p style="color:#666;font-size:12px;">
        来自 X Monitor Bot · 运行在 GitHub Actions 上
    </p>
    </body></html>
    """
    return send_email(subject, body)


# ════════════════════════════════════════════════
#  去重 & 状态管理
# ════════════════════════════════════════════════

def load_sent_tweets() -> set[str]:
    """加载已发送的推文 ID 集合"""
    if SENT_TWEETS_FILE.exists():
        try:
            data = json.loads(SENT_TWEETS_FILE.read_text(encoding="utf-8"))
            # 新格式：{"tweet_ids": ["123", "456"], "last_updated": "..."}
            # 旧格式兼容：["123", "456"]
            if isinstance(data, dict):
                ids = data.get("tweet_ids", [])
            else:
                ids = data
            # 只保留最近的 500 条记录，防止文件过大
            return set(ids[-500:])
        except (json.JSONDecodeError, TypeError):
            pass
    return set()


def save_sent_tweets(tweet_ids: set[str]) -> None:
    """保存已发送的推文 ID"""
    data = {
        "tweet_ids": list(tweet_ids)[-500:],
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    SENT_TWEETS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def deduplicate_tweets(tweets: list[dict]) -> list[dict]:
    """
    去重：过滤掉已发送过的推文。
    返回的是未发送过的新推文。
    """
    sent = load_sent_tweets()
    new_tweets = []
    new_ids = set()

    for tweet in tweets:
        tid = tweet.get("id", "") or tweet.get("rest_id", "")
        if tid and tid not in sent:
            new_tweets.append(tweet)
            new_ids.add(tid)
            sent.add(tid)

    if new_ids:
        save_sent_tweets(sent)
        log(f"发现 {len(new_ids)} 条新推文（共 {len(tweets)} 条, 已过滤 {len(tweets) - len(new_ids)} 条重复）")
    else:
        log(f"没有发现新推文（{len(tweets)} 条均为已发送）")

    return new_tweets


# ════════════════════════════════════════════════
#  邮件模板
# ════════════════════════════════════════════════

def build_email_html(tweets: list[dict], target_user: str) -> str:
    """构建推文通知邮件的 HTML 内容"""
    if not tweets:
        return ""

    utc8 = timezone(timedelta(hours=8))

    tweet_count = len(tweets)
    subject_hint = (
        f"发布了 {tweet_count} 条推文"
        if tweet_count > 1
        else "发布了 1 条推文"
    )

    tweet_cards = ""
    for t in tweets:
        text = (t.get("full_text") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # 将换行转为 <br>
        text = text.replace("\n", "<br>")
        # 将 URL 转为可点击链接
        text = re.sub(
            r'(https?://\S+)',
            r'<a href="\1" style="color:#1d9bf0;">\1</a>',
            text,
        )

        # 解析推文发布时间
        created_str = t.get("created_at", "")
        try:
            # X API 的时间格式: "Wed Oct 05 20:30:00 +0000 2022"
            created_dt = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
            created_beijing = created_dt.astimezone(utc8)
            time_display = created_beijing.strftime("%Y-%m-%d %H:%M:%S") + " (北京时间)"
        except (ValueError, KeyError):
            time_display = created_str or "未知时间"

        tweet_cards += f"""
        <div style="margin-bottom:24px; padding:16px 20px;
                    border:1px solid #e1e8ed; border-radius:12px;
                    background:#ffffff;">
            <!-- 推文正文 -->
            <div style="font-size:15px; line-height:1.6; color:#0f1419; margin-bottom:12px;">
                {text}
            </div>

            <!-- 时间 & 链接 -->
            <div style="font-size:13px; color:#536471; margin-bottom:10px;">
                🕐 {time_display}
            </div>

            <div style="margin-top:12px; display:flex; gap:24px; font-size:13px; color:#536471;">
                <span>💬 {t.get('reply_count', 0):,}</span>
                <span>🔁 {t.get('retweet_count', 0):,}</span>
                <span>❤️ {t.get('favorite_count', 0):,}</span>
                <span>👁 {format_view_count(t.get('view_count', 'N/A'))}</span>
            </div>

            <div style="margin-top:14px;">
                <a href="{t.get('url', '#')}"
                   style="display:inline-block; padding:8px 18px;
                          background:#1d9bf0; color:#fff; text-decoration:none;
                          border-radius:20px; font-size:13px; font-weight:500;">
                    🔗 查看原推文
                </a>
            </div>
        </div>
        """

    # 完整 HTML
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin:0;padding:20px;background:#f7f9fa;
                font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <div style="max-width:600px;margin:0 auto;">

            <!-- 头部 -->
            <div style="background:linear-gradient(135deg,#1d9bf0,#0c7abf);
                        padding:24px 28px; border-radius:16px 16px 0 0; color:#fff;">
                <h1 style="margin:0;font-size:22px;">🐦 X 推文提醒</h1>
                <p style="margin:8px 0 0;font-size:14px;opacity:0.9;">
                    <a href="https://x.com/{target_user}"
                       style="color:#fff;text-decoration:none;font-weight:600;">
                        @{target_user}
                    </a>
                    &nbsp;{subject_hint}
                </p>
            </div>

            <!-- 推文列表 -->
            <div style="background:#fff;padding:24px 28px;
                        border-left:1px solid #e1e8ed;border-right:1px solid #e1e8ed;">
                {tweet_cards}
            </div>

            <!-- 底部 -->
            <div style="background:#f7f9fa;padding:18px 28px;
                        border:1px solid #e1e8ed;border-radius:0 0 16px 16px;
                        text-align:center;font-size:11px;color:#8899a6;">
                <p style="margin:0;">
                    📬 来自 X Monitor Bot · 下次检查: 约4-6小时后
                    <br>运行在 GitHub Actions · 100% 免费 · 无 API 费用
                </p>
            </div>
        </div>
    </body>
    </html>
    """
    return html


def format_view_count(count) -> str:
    """格式化查看次数"""
    if isinstance(count, str):
        return count
    try:
        c = int(count)
        if c >= 10000:
            return f"{c/10000:.1f}万"
        return f"{c:,}"
    except (ValueError, TypeError):
        return "N/A"


def get_email_subject(tweets: list[dict], target_user: str) -> str:
    """生成邮件标题"""
    n = len(tweets)
    if n == 0:
        return f"[X监控] @{target_user} · 无新推文"
    elif n == 1:
        # 截取前30字作为标题亮点
        text = tweets[0].get("full_text", "")[:30]
        return f"[X监控] @{target_user} 新推文: {text}..."
    else:
        return f"[X监控] @{target_user} 发布了 {n} 条新推文"


# ════════════════════════════════════════════════
#  主逻辑
# ════════════════════════════════════════════════

def check_and_notify() -> dict:
    """
    主流程：
    1. 抓取推文
    2. 去重
    3. 发送邮件
    返回运行摘要。
    """
    summary = {
        "target": TARGET_SCREEN_NAME,
        "fetched": 0,
        "new": 0,
        "email_sent": False,
        "error": None,
    }

    # ── Step 1: 抓取 ──
    tweets = fetch_tweets(TARGET_SCREEN_NAME)

    # 如果主方案失败，尝试 RSS 备选
    if not tweets:
        log("主方案未获取到推文，尝试备选方案...")
        tweets = fetch_tweets_guest_fallback(TARGET_SCREEN_NAME)

    summary["fetched"] = len(tweets)

    if tweets:
        # 按时间排序（最新的在前）
        # X API 返回的一般已经排好序，但确保一下
        tweets.sort(key=lambda t: t.get("id", ""), reverse=True)

    # ── Step 2: 去重 ──
    new_tweets = deduplicate_tweets(tweets)
    summary["new"] = len(new_tweets)

    # ── Step 3: 发送邮件 ──
    if new_tweets:
        html = build_email_html(new_tweets, TARGET_SCREEN_NAME)
        subject = get_email_subject(new_tweets, TARGET_SCREEN_NAME)
        success = send_email(subject, html)

        if success:
            summary["email_sent"] = True
            log(f"✓ 完成: {len(new_tweets)} 条新推文已发送到邮件")
        else:
            summary["error"] = "邮件发送失败"
            log("✗ 邮件发送失败", "ERROR")
    else:
        log("✓ 没有新推文，跳过邮件发送")

    return summary


# ════════════════════════════════════════════════
#  Cookies 获取助手（本地交互式）
# ════════════════════════════════════════════════

def interactive_login_guide():
    """
    交互式引导用户获取 X cookies。
    """
    print("\n" + "=" * 60)
    print("  🔐 X (Twitter) Cookies 获取向导")
    print("=" * 60)
    print("""
由于 X 需要登录才能访问推文 API，你需要手动获取浏览器 cookies。

📋 操作步骤（大约需要 3 分钟）:

  1. 打开 Chrome/Firefox 浏览器
  2. 登录 https://x.com（如果在中国大陆，需要代理/VPN）
  3. 登录后，按 F12 打开开发者工具
  4. 切换到 "Application" (Chrome) 或 "Storage" (Firefox) 标签
  5. 在左侧找到 "Cookies" → "https://x.com"
  6. 导出 cookies:

     方法A - 浏览器插件（推荐）:
       · 安装 "EditThisCookie" 或 "Cookie-Editor" 插件
       · 点击插件图标 → Export → 复制全部 JSON 内容

     方法B - 手动复制关键 cookies:
       · 在开发者工具的 cookies 列表中
       · 找到并复制以下关键 cookie 的值:
         - auth_token  （最重要！）
         - ct0
       · 我会帮你构建成 JSON 格式

  7. 将 cookies JSON 粘贴到下方输入
""")

    print("-" * 60)
    choice = input("选择方式 [A=插件导出JSON / B=手动输入关键cookie]: ").strip().upper()

    if choice == "B":
        print("\n请依次输入关键 cookie 值（直接回车跳过）:\n")
        auth_token = input("  auth_token: ").strip()
        ct0 = input("  ct0: ").strip()

        if not auth_token:
            print("\n❌ auth_token 是必需的，未提供。")
            return

        cookies = [
            {"name": "auth_token", "value": auth_token, "domain": ".x.com"},
        ]
        if ct0:
            cookies.append({"name": "ct0", "value": ct0, "domain": ".x.com"})

        cookies_json = json.dumps(cookies, indent=2, ensure_ascii=False)
        print("\n生成的 cookies JSON:")
        print("-" * 40)
        print(cookies_json)
        print("-" * 40)

    else:
        print("\n请粘贴从插件导出的完整 cookies JSON 数组")
        print("（粘贴完成后按 Enter，然后按 Ctrl+D (Linux/Mac) 或 Ctrl+Z (Windows) 结束输入）:\n")
        lines = []
        try:
            while True:
                line = input()
                lines.append(line)
        except (EOFError, KeyboardInterrupt):
            pass
        cookies_json = "\n".join(lines)

    # 验证 JSON
    try:
        cookies = json.loads(cookies_json)
        if isinstance(cookies, dict):
            cookies = [cookies]
        if not isinstance(cookies, list):
            print("\n❌ 格式错误：需要 JSON 数组格式")
            return
        print(f"\n✅ JSON 格式正确，共 {len(cookies)} 个 cookies。")
    except json.JSONDecodeError as e:
        print(f"\n❌ JSON 解析失败: {e}")
        print("请确保粘贴的内容是有效的 JSON 格式。")
        return

    # 保存到文件
    COOKIES_FILE.write_text(
        json.dumps(cookies, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"✅ Cookies 已保存到: {COOKIES_FILE}")

    # 提示 GitHub Actions 配置
    print(f"""
{"=" * 60}
📌 下一步: 设置 GitHub Actions 环境变量
{"=" * 60}

如果你要部署到 GitHub Actions，需要将 cookies 设为 Secret:

  1. 复制下面的值:
{'-' * 56}
{json.dumps(cookies, ensure_ascii=False)}
{'-' * 56}

  2. 进入 GitHub 仓库 → Settings → Secrets and variables → Actions
  3. 点击 "New repository secret"
  4. Name:  X_COOKIES
  5. Value: 粘贴上面那行 JSON
  6. 点击 "Add secret"

  同样添加:
    · SENDER_EMAIL      → 你的 Outlook 邮箱
    · SENDER_PASSWORD   → Outlook 应用密码
    · RECIPIENT_EMAIL   → 接收通知的邮箱
    · TARGET_SCREEN_NAME → 要监控的博主用户名

  详细说明请参考安装指南！
""")


# ════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="X (Twitter) 推文监控 + 邮件转发",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python main.py                       # 运行一次检查
  python main.py --login               # 交互式获取 cookies
  python main.py --startup             # 发送启动通知邮件
  python main.py --test                # 测试邮件发送功能
  python main.py --verbose             # 显示详细日志
        """,
    )
    parser.add_argument(
        "--login", action="store_true",
        help="交互式设置 X cookies",
    )
    parser.add_argument(
        "--test-email", action="store_true",
        help="发送一封测试邮件，验证 SMTP 配置",
    )
    parser.add_argument(
        "--startup", action="store_true",
        help="发送监控启动通知",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="显示详细调试日志",
    )
    parser.add_argument(
        "--target", type=str, default="",
        help="临时指定目标用户（覆盖环境变量）",
    )

    args = parser.parse_args()

    # 设置全局配置
    global VERBOSE, TARGET_SCREEN_NAME
    if args.verbose:
        VERBOSE = True
    if args.target:
        TARGET_SCREEN_NAME = args.target

    # ── 交互式登录 ──
    if args.login:
        interactive_login_guide()
        return

    # ── 测试邮件 ──
    if args.test_email:
        print("\n📧 发送测试邮件...")
        test_html = f"""
        <html><body>
        <h2>🧪 测试邮件</h2>
        <p>如果你收到这封邮件，说明 Outlook SMTP 配置正确！</p>
        <p><strong>配置检查:</strong></p>
        <ul>
            <li>发件人: {SENDER_EMAIL}</li>
            <li>SMTP: {SMTP_SERVER}:{SMTP_PORT}</li>
            <li>收件人: {RECIPIENT_EMAIL}</li>
            <li>目标用户: @{TARGET_SCREEN_NAME}</li>
        </ul>
        <pre>X_COOKIES 状态: {'✅ 已配置' if (os.getenv('X_COOKIES') or COOKIES_FILE.exists()) else '❌ 未配置'}</pre>
        <hr>
        <p style="color:#666;font-size:12px;">X Monitor Bot · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </body></html>
        """
        success = send_email("🧪 [X监控] 测试邮件", test_html)
        if success:
            print("✅ 测试邮件发送成功！\n")
        else:
            print("❌ 测试邮件发送失败，请检查配置。\n")
        return

    # ── 启动通知 ──
    if args.startup:
        print("\n📬 发送启动通知...")
        send_startup_notification()
        return

    # ── 主流程 ──
    print("\n" + "=" * 50)
    print(f"  🐦 X 推文监控")
    print(f"  目标: @{TARGET_SCREEN_NAME}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50 + "\n")

    try:
        summary = check_and_notify()
        print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        err_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        log(f"脚本运行异常: {err_msg}", "ERROR")
        # 尝试发送错误通知
        try:
            send_error_notification(err_msg)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()

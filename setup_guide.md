# 🐦 X (Twitter) 推文监控 + 邮件转发 — 安装指南

> **零费用 · 自动运行 · 无需 API Key · 无需 VPN（服务端运行在 GitHub Actions 境外服务器）**

---

## 📋 方案原理

```
GitHub Actions (每6小时自动触发, 美国服务器)
  └─ Python 脚本 爬取 X 推文
       └─ 对比上次结果 (去重)
            └─ 新推文 → QQ邮箱 SMTP → 你的邮箱 📬
```

**为什么不需要 VPN？** 因为脚本运行在 GitHub Actions 的美国服务器上，直接访问 X.com。QQ 邮箱在国内收发完全正常。

---

## 🔧 第一步：获取 QQ 邮箱授权码（1 分钟搞定）

脚本通过 SMTP 协议发送邮件，需要 QQ 邮箱的「授权码」。

1. 浏览器打开 https://mail.qq.com → 登录你的 QQ 邮箱
2. 点击顶部 **设置** → 切换到 **账户** 标签
3. 往下滚动找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务**
4. 找到 **SMTP 服务** → 点击 **开启**（如果已开启则跳过）
5. QQ 会让你发一条短信验证 → 发送后点「我已发送」
6. ✅ 弹出一个 **16 位授权码**，复制保存（类似 `abcdefghijklmnop`）

> ⚠️ **这个授权码不是你的 QQ 密码！** 是一个单独的 16 位字符串。保存好，后续要用。

---

## 🔐 第二步：获取 X (Twitter) Cookies

由于 X 限制访客访问，需要登录后的 cookies。**这一步只需做一次**，可以在本地完成。

### 方案 A：浏览器插件（推荐，1 分钟）

1. 打开 Chrome → 登录 https://x.com
2. 安装插件 **"EditThisCookie"** 或 **"Cookie-Editor"**
3. 登录 X 后，点插件图标 → **Export**（导出）
4. 复制全部 JSON 内容备用

### 方案 B：脚本交互式（本地运行）

```bash
cd D:\PCproject
pip install httpx
python main.py --login
```

按照提示操作即可。

---

## 🚀 第三步：部署到 GitHub Actions

### 3.1 创建 GitHub 仓库

1. 打开 https://github.com/new
2. 仓库名随意，如 `x-monitor`
3. **⚠️ 建议设为 Private（私有）**，因为 cookies 是敏感信息
4. 创建后，将代码推送到仓库：

```bash
cd D:\PCproject
git init
git add .
git commit -m "初始提交: X推文监控"
git branch -M main
git remote add origin https://github.com/你的用户名/x-monitor.git
git push -u origin main
```

### 3.2 配置 Secrets（5 个）

进入仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，依次添加：

| Secret 名称 | 值 | 说明 |
|-------------|-----|------|
| `SENDER_EMAIL` | `123456789@qq.com` | 你的 QQ 邮箱 |
| `SENDER_PASSWORD` | `abcdefghijklmnop` | QQ 邮箱授权码（第一步获取的 16 位） |
| `RECIPIENT_EMAIL` | `123456789@qq.com` | 接收推文通知的邮箱（可以和发件人相同） |
| `X_COOKIES` | `[{"name":"auth_token","value":"xxx"...}]` | 第二步获取的 cookies JSON |
| `TARGET_SCREEN_NAME` | `elonmusk` | 要监控的博主用户名（不带 @） |

> 🔒 **Secrets 是加密存储的，GitHub Actions 运行时解密注入环境变量，非常安全。**

> 💡 QQ 邮箱也可以发到其他邮箱（如 163、Gmail），只需要把 `RECIPIENT_EMAIL` 设为目标邮箱即可。

### 3.3 手动测试运行

1. 仓库 → **Actions** 标签 → 左侧选择 **X-Tweet-Monitor**
2. 点击 **Run workflow** → **Run workflow**（绿色按钮）
3. 等待 1-2 分钟，查看运行日志
4. 如果成功，你的 QQ 邮箱会收到一封邮件 ✉️

---

## 🎛️ 自定义配置

### 调整检查频率

编辑 `.github/workflows/scrape.yml` 中的 `cron`：

```yaml
schedule:
  # 当前: 每6小时 (每天4次, ~240分钟/月)
  - cron: '0 */6 * * *'
  
  # 改为每3小时 (每天8次, ~480分钟/月) — 仍在免费额度内
  # - cron: '0 */3 * * *'
  
  # 改为每小时 (每天24次, ~1440分钟/月) — 接近但未超限额
  # - cron: '0 * * * *'
```

> GitHub Actions 免费额度：**2000 分钟/月**（私有仓库）。每次运行约 1-2 分钟。

### 换用其他邮箱

脚本默认使用 QQ 邮箱，但如果你之后想换其他邮箱：

```bash
# 163 邮箱
SMTP_SERVER=smtp.163.com  SMTP_PORT=465
# Outlook  
SMTP_SERVER=smtp-mail.outlook.com  SMTP_PORT=587
# Gmail
SMTP_SERVER=smtp.gmail.com  SMTP_PORT=587
```

在 GitHub Secrets 中额外添加 `SMTP_SERVER` 和 `SMTP_PORT` 即可覆盖默认值。

### 监控多个博主

方法一：复制 workflow 中的 job，改 `TARGET_SCREEN_NAME`  
方法二：修改脚本，用逗号分隔多个用户名，循环抓取

---

## 🧪 本地测试命令

```bash
# 安装依赖
pip install httpx

# 测试邮件发送（最重要的一步！先跑这个确认 QQ 邮箱配置正确）
set SENDER_EMAIL=你的QQ号@qq.com
set SENDER_PASSWORD=你的QQ邮箱授权码
set RECIPIENT_EMAIL=接收邮件的邮箱
python main.py --test-email

# 设置 cookies（交互式）
python main.py --login

# 发送启动通知
python main.py --startup

# 完整运行一次（显示详细日志）
python main.py --verbose
```

> 本地运行时需要能访问 X.com。如果抓取失败，部署到 GitHub Actions 后就好了（GitHub 服务器在美国）。

---

## 🔄 Cookies 过期处理

X 的 cookies 有效期通常为 **几周到几个月**。如果过期了，你会收到邮件通知。

**更新方法：**
1. 在浏览器重新登录 X
2. 重新导出 cookies（第二步）
3. 更新 GitHub Secrets 中的 `X_COOKIES`

---

## 📊 月度成本一览

| 项目 | 费用 |
|------|------|
| GitHub Actions | ¥0（每6小时共约240分钟/月，远低于2000分钟限额） |
| QQ 邮箱 SMTP | ¥0（QQ 邮箱免费，每天发送量绰绰有余） |
| X 抓取 | ¥0（无官方 API 费用） |
| **总计** | **¥0** |

---

## ❓ 常见问题

**Q: 为什么我在本地跑报错 "无法获取用户 ID"？**  
A: 本地可能无法访问 X.com。部署到 GitHub Actions 后就好了（GitHub 服务器在美国）。

**Q: QQ 邮箱收不到邮件？**  
A: ①检查垃圾邮件文件夹 ②确认用的是「授权码」而不是 QQ 密码 ③SMTP 端口是 465。

**Q: QQ 邮箱授权码在哪里？**  
A: QQ邮箱 → 设置 → 账户 → POP3/SMTP服务 → 开启 → 发送短信 → 获取16位授权码。

**Q: 想同时监控多个博主？**  
A: 可以修改脚本，用逗号分隔的用户名列表，循环抓取。

**Q: GitHub Actions 会被封吗？**  
A: 每6小时十几条推文的抓取量，和正常用户浏览无异，不会被封。

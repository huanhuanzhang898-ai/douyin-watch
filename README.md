# 抖音博主更新提醒（推送到微信）

博主发了新视频，微信里会收到一条消息：博主名、发布时间、视频文案，加上一个只打开这一个视频的链接。
不用装抖音 App，平时也不用开电脑，脚本在 GitHub 的服务器上每 30 分钟自动检查一次。

> 第一次设置建议在电脑上做，大约 20 分钟。

---

## 第 1 步：拿到微信推送的密钥（二选一）

**PushPlus（推荐，每天免费额度比较多）**
1. 打开 https://www.pushplus.plus ，用微信扫码登录，并关注它的公众号。
2. 如果网站提示需要实名认证，按提示完成。
3. 在“一对一推送”里复制你的 **token**。

**Server酱（备选，免费版每天 5 条，只关注一两个博主也够用）**
1. 打开 https://sct.ftqq.com ，用微信扫码登录。
2. 在“SendKey”页面复制你的 **SendKey**。

## 第 2 步：复制抖音 Cookie（必须用电脑浏览器）

1. 用 Chrome 或 Edge 打开 https://www.douyin.com 并**登录**。
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）** 标签。
3. 按 `F5` 刷新页面，在列表里点第一个请求（名字通常是 `www.douyin.com` 或 `/`）。
4. 在右侧 **Headers（标头）→ Request Headers（请求标头）** 里找到 `cookie:`，把它后面**一整串**都复制下来。

> Cookie 相当于你的登录凭证，只填到 GitHub 的 Secrets 里，不要发给任何人。

## 第 3 步：复制博主主页链接

在电脑上用抖音网页版搜索这个博主，打开他的主页，复制地址栏里的链接，格式类似：
`https://www.douyin.com/user/MS4wLjABAAAA……`

（如果拿到的是手机分享的短链接 `https://v.douyin.com/xxxx/`，也可以直接用。）

## 第 4 步：在 GitHub 建仓库并上传文件

1. 注册或登录 https://github.com 。
2. 右上角 **+ → New repository**，名字随便起（比如 `douyin-watch`），**选 Private（私有）**，点 Create。
3. 点 **Add file → Upload files**，上传 `douyin_watch.py` 和 `README.md`。
4. 再点 **Add file → Create new file**，文件名一栏输入 `.github/workflows/watch.yml`（输入斜杠时会自动变成文件夹），把 `watch.yml` 的内容粘贴进去，点 Commit。

## 第 5 步：填写密钥（Secrets）

在仓库页面点 **Settings → Secrets and variables → Actions → New repository secret**，逐个添加：

| Name | 填什么 |
|---|---|
| `DOUYIN_USERS` | 第 3 步的博主主页链接，多个博主就每行一个 |
| `DOUYIN_COOKIE` | 第 2 步复制的那一整串 Cookie |
| `PUSHPLUS_TOKEN` | 第 1 步的 PushPlus token（用 Server酱 的话就不填这个） |
| `SERVERCHAN_KEY` | 第 1 步的 Server酱 SendKey（用 PushPlus 的话就不填这个） |

## 第 6 步：第一次手动运行

1. 点仓库上方的 **Actions**，如果有提示，点绿色按钮启用。
2. 左边点 **抖音更新提醒 → Run workflow → Run workflow**。
3. 等 1 分钟左右，微信应该收到一条 **“抖音监控已启动”**，里面有博主最近一条视频的文案，说明一切正常。
4. 之后它会每 30 分钟自动检查一次，没有新视频时不会打扰你。

---

## 常见问题

**微信收到“抓取失败”**
多半是 Cookie 过期了。重新做一遍第 2 步，在 Secrets 里更新 `DOUYIN_COOKIE`（点它右边的铅笔图标）即可。

**第一次运行就失败、没收到“已启动”**
- 点开那次运行，看 **检查更新** 这一步的日志，把报错发给 Claude。
- 运行页面最下面如果有 **debug-screenshot**，下载看看截图：如果是验证码或登录页，说明抖音不让 GitHub 的海外服务器访问，需要换成在自己电脑或国内服务器上运行。

**保存状态这一步报错 `Permission denied` / 403**
到 **Settings → Actions → General → Workflow permissions**，选 **Read and write permissions**，保存后再运行一次。

**想改检查频率**
修改 `watch.yml` 里的 `*/30`（单位是分钟）。私有仓库每月有 2000 分钟免费额度，每次运行按整分钟计，每 30 分钟一次大约用 1500 到 2000 分钟，**不建议改得更频繁**。
可以在 GitHub 头像 → **Settings → Billing** 里看用量；如果快用完，就把 `*/30 * * * *` 改成 `0 * * * *`（每小时一次）。
另外 GitHub 的定时任务在高峰期会延迟，偶尔也会跳过一次，所以提醒通常比博主发布晚几分钟到半小时左右。

**同一个视频会不会重复提醒？**
不会。已提醒过的视频会记在仓库里的 `state.json` 中。博主把很早的旧视频重新置顶也不会提醒（只提醒 48 小时内发布的）。

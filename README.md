# JM2 — JMComic QQ Bot（Linux 版）

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 构建的 QQ 群机器人，
通过 [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11 协议）接入 QQ，
提供禁漫本子搜索、详情查询、下载转 PDF 等功能。

## 🎬 演示视频

[![2000字！宇宙安全声明合集](https://i2.hdslb.com/bfs/archive/b1e8f5a3c4d2e6a7f901234567890abcdef.jpg)](https://www.bilibili.com/video/BV1Ts4y1F7r3/)

> 📺 [2000字！宇宙安全声明合集 — 哔哩哔哩](https://www.bilibili.com/video/BV1Ts4y1F7r3/)

## 🤖 体验 QQ

| 项目 | 详情 |
|------|------|
| 体验 QQ | **2837430647**（JMBot） |

| 触发方式 | `@JMBot` 或 `/` 指令 |

## 功能

### `/` 指令（无需 @Bot，直接使用）

| 命令 | 说明 | 示例 |
|------|------|------|
| `/search <ID>` | 按车号精确查询本子详情 | `/search 350234` |
| `/search <关键词> [参数...]` | 关键词搜索，支持排序/分页 | 见下方 |
| `/download <ID> [ID...] [-s]` | 下载本子并发送 PDF | `/download 350234` |
| `/download <ID> -s` | 下载但不发送（仅存储） | `/download 350234 -s` |
| `/top [日榜\|周榜\|月榜]` | 查看排行榜 | `/top 周榜` |
| `/info <ID>` | 精简本子信息 | `/info 350234` |
| `/cover <ID>` | 查看封面图片 | `/cover 350234` |
| `/random` | 随机推荐 | `/random` |
| `/recent` | 最近查询（10个） | `/recent` |
| `/fav add\|list\|remove <ID>` | 收藏管理 | `/fav add 350234` |
| `/rating <ID> <1-10>` | 评分 | `/rating 350234 8` |
| `/stats` | 已下载/待下载统计 | `/stats` |
| `/sysinfo` | CPU/内存/磁盘信息 | `/sysinfo` |
| `/prefs [set sort=xxx top=xx]` | 搜索偏好设置 | `/prefs set sort=最新 top=10` |
| `/jmurl` | 获取禁漫地址（防和谐） | `/jmurl` |
| `/ping` | Bot 存活检测 | `/ping` |
| `/help` | 显示完整帮助 | `/help` |

### 🧠 自然语言对话（需要 `@JMBot`）

| 触发示例 | 效果 |
|----------|------|
| `@JMBot 帮我找原神本子` | 搜索「原神」 |
| `@JMBot 有没有纯爱的最新本子` | 搜索「纯爱」，按最新排序 |
| `@JMBot 推荐一个` | 随机推荐 |
| `@JMBot 日榜` | 查看日榜 TOP5 |
| `@JMBot 帮我找傲娇系雌小鬼纯爱` | 自动拆分为多个标签搜索 |

支持手打 `@bot` / `@JMBot` / `@jmBot`，也支持 QQ 内置 @提及。

### 快捷操作（搜索结果后可用）

| 回复 | 说明 |
|------|------|
| `n` | 下一页 |
| `p` | 上一页 |
| `d1` | 下载第1个结果 |
| `d1-3` | 下载第1-3个结果 |

### 搜索参数

支持两种格式：

**旧格式（位置参数）**：
```
/search 原神 最新 10       → sort=最新, top=10
/search 无修正 5            → top=5
```

**新格式（key=value，支持分页和类型）**：
```
/search 原神 sort=观看 top=10 page=2
/search MANA type=author top=5
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `sort=` | 排序：收藏/最新/观看/长度 | `收藏` |
| `top=` 或 `page_size=` | 每页条数 | `20` |
| `page=` | 页码（翻页用） | `1` |
| `type=` | normal / author(作者) / tag(标签) | `normal` |

### 其他特性

- **群白名单**：通过 `ALLOWED_GROUPS` 环境变量限制可用群聊
- **相关性搜索**：多关键词自动转为 `+词1 +词2`，大幅提升搜索精度
- **1 分 50 秒自动撤回**：每条 Bot 消息在发送后 110 秒自动删除
- **后台静默下载**：自动从 albums.log 中下载未下载过的本子，限速去重
- **智能车号识别**：聊天中提及 JM123456 自动查询
- **聊天记录**：按群/私聊分文件保存到 `logs/chat/`
- **使用日志**：`logs/usage.log` + `logs/albums.log`（含标题和标签）

## 环境要求

- Linux（推荐 Ubuntu 20.04+）
- Python >= 3.8
- NapCat（QQ Bot 框架，基于 QQ Linux）

## 快速开始

### 1. 安装依赖

```bash
pip install jmcomic flask requests img2pdf psutil
cd JM2 && pip install -e . --no-build-isolation
```

### 2. 安装 NapCat

```bash
bash napcat.sh
```

### 3. 启动 NapCat

```bash
screen -dmS napcat bash -c "xvfb-run -a /opt/QQ/qq --no-sandbox"
# 首次需扫码登录，之后可用 -q QQ号 快速登录
```

### 4. 配置 OneBot

NapCat WebUI 中启用 HTTP 服务（端口 3000）并添加 HTTP 上报到 `http://127.0.0.1:9001/onebot`。
或直接编辑 `onebot11_<QQ号>.json` 配置文件。

### 5. 启动 Bot

```bash
cd JM2
BOT_QQ=你的QQ号 ALLOWED_GROUPS=群号1,群号2 \
  PYTHONUNBUFFERED=1 nohup python3 -u bot_download_server.py > /tmp/bot.log 2>&1 &
```

或使用快捷脚本：
```bash
./restart_bot.sh   # 重启
./stop_bot.sh      # 停止
```

## 环境变量

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `PORT` | `9001` | Bot 监听端口 |
| `BOT_QQ` | — | 机器人 QQ 号（用于 @mention 检测） |
| `ALLOWED_GROUPS` | 空=不限制 | 群白名单（逗号分隔） |
| `ONEBOT_BASE_URL` | `http://127.0.0.1:3000` | NapCat OneBot API 地址 |
| `DOWNLOAD_OUTPUT_DIR` | `./downloads` | PDF 输出目录 |
| `JM_OPTION_PATH` | — | jmcomic 配置文件路径 |

## 项目结构

```
JM2/
├── bot_download_server.py   # QQ Bot 主程序（~1400 行）
├── search_album_info.py     # 搜索脚本
├── download_album_to_pdf.py # 下载 + PDF 生成
├── napcat.sh                # NapCat 安装脚本
├── restart_bot.sh           # 紧急重启脚本
├── stop_bot.sh              # 紧急停止脚本
├── downloads/               # PDF 下载输出（gitignore）
├── logs/
│   ├── usage.log            # 命令使用记录
│   ├── albums.log           # 本子访问记录（含标题+标签）
│   ├── downloaded.txt       # 已下载 ID
│   └── chat/                # 聊天记录（分群/私聊）
├── src/jmcomic/             # jmcomic 核心库
└── README.md
```

## 致谢

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python)
- [NapCat](https://github.com/NapNeko/NapCatQQ)
- [Bilibili 演示视频](https://www.bilibili.com/video/BV1Ts4y1F7r3/)

## 免责声明

本项目仅供学习交流使用，请遵守相关法律法规，合理使用。

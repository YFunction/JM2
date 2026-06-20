# JM2 — JMComic QQ Bot（Linux 版）

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 构建的 QQ 群机器人，
通过 [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11 协议）接入 QQ，
提供禁漫本子搜索、详情查询、下载转 PDF 等功能。

## 功能

| 命令 | 说明 | 示例 |
|------|------|------|
| `/search <ID>` | 按车号精确查询本子详情 | `/search 350234` |
| `/search <关键词> [排序] [数量]` | 关键词搜索，多词自动相关性匹配 | `/search 无修正 收藏 20` |
| `/download <ID>` | 下载本子并生成 PDF | `/download 350234` |
| `/jmurl` | 获取禁漫地址（防和谐格式） | `/jmurl` |
| `/ping` | Bot 存活检测 | `/ping` |
| `/help` | 显示完整帮助 | `/help` |

### 搜索排序参数

在 `/search` 关键词后可选指定排序和数量（默认按收藏排序、显示前 20 条）：

| 参数 | 含义 |
|------|------|
| `收藏` / `最新` / `观看` / `长度` | 排序方式 |
| 数字（1-50） | 显示条数 |

示例：
```
/search 无修正              # 收藏排序，前 20 条
/search 无修正 最新 5        # 发布时间排序，前 5 条
/search 无修正 观看 10       # 观看次数排序，前 10 条
```

### 其他特性

- **松散命令匹配**：支持 `@bot /search xxx` 或 `帮我查一下 /search xxx` 等自然语言
- **相关性搜索**：多关键词自动转为 `+词1 +词2`，大幅提升搜索精度
- **1 分 50 秒自动撤回**：每条 Bot 消息在发送后 110 秒自动删除
- **使用日志**：`logs/usage.log` 记录所有命令，`logs/albums.log` 记录访问过的本子

## 环境要求

- Linux（推荐 Ubuntu 20.04+）
- Python >= 3.8
- NapCat（QQ Bot 框架，基于 QQ Linux）

## 快速开始

### 1. 安装依赖

```bash
# 安装 Python 包
pip install jmcomic flask requests img2pdf

# 或从源码安装 jmcomic
cd JM2
pip install -e . --no-build-isolation
```

### 2. 安装 NapCat

```bash
# 运行安装脚本
bash napcat.sh
```

### 3. 启动 NapCat

```bash
# 虚拟桌面 + QQ 无沙箱启动
screen -dmS napcat bash -c "xvfb-run -a /opt/QQ/qq --no-sandbox"

# 首次启动需扫码登录，之后可用 -q 快速登录
screen -dmS napcat bash -c "xvfb-run -a /opt/QQ/qq --no-sandbox -q QQ号"
```

### 4. 配置 OneBot

浏览器打开 NapCat WebUI：`http://127.0.0.1:6099/webui?token=<token>`

- 启用 **HTTP 服务**（端口 3000）
- 添加 **HTTP 上报**，地址设为 `http://127.0.0.1:9001/onebot`

> 或直接编辑配置文件 `onebot11_<QQ号>.json`：
> ```json
> {
>   "network": {
>     "httpServers": [{"enable": true, "host": "0.0.0.0", "port": 3000}],
>     "httpClients": [{"enable": true, "url": "http://127.0.0.1:9001/onebot"}]
>   }
> }
> ```

### 5. 启动 Bot

```bash
cd JM2
PYTHONUNBUFFERED=1 nohup python3 -u bot_download_server.py > /tmp/bot.log 2>&1 &
```

Bot 默认监听 `0.0.0.0:9001`，通过环境变量可配置：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `PORT` | `9001` | Bot 监听端口 |
| `ONEBOT_BASE_URL` | `http://127.0.0.1:3000` | NapCat OneBot API 地址 |
| `DOWNLOAD_OUTPUT_DIR` | `./my_pdf_output` | PDF 输出目录 |
| `JM_OPTION_PATH` | — | jmcomic 配置文件路径 |

## 项目结构

```
JM2/
├── bot_download_server.py   # QQ Bot 主程序
├── search_album_info.py     # 搜索脚本（ID 查询 / 关键词搜索）
├── download_album_to_pdf.py # 下载 + PDF 生成脚本
├── napcat.sh                # NapCat 安装脚本
├── logs/                    # 运行日志
│   ├── usage.log            # 命令使用记录
│   └── albums.log           # 本子访问记录
├── src/jmcomic/             # jmcomic 核心库（上游）
│   ├── api.py               # 下载 API
│   ├── jm_client_impl.py    # 移动端 / 网页端客户端
│   ├── jm_plugin.py         # 插件（img2pdf 等）
│   └── ...
└── README.md
```

## 致谢

- [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) — 禁漫 Python API
- [NapCat](https://github.com/NapNeko/NapCatQQ) — QQ Bot 框架

## 免责声明

本项目仅供学习交流使用，请遵守相关法律法规，合理使用。
不要一次性爬取太多本子，请珍爱 JM 服务器。



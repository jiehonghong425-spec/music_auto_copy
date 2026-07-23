# 🎵 音乐处理平台 v2

批量下载 + AI 人声分离 + Web 管理界面，一站式音乐处理工具。

## v2 新特性

- 🌐 **Web 管理界面** — FastAPI 驱动的全功能 Web UI，告别命令行
- 🇨🇳 **国内音乐平台支持** — 网易云音乐 / QQ音乐 / 酷狗音乐搜索与下载
- 📤 **本地上传** — 支持音频/视频文件上传，自动提取视频中的音频
- 🔄 **格式转换** — WAV / MP3 / FLAC / OGG 互转（FFmpeg）
- 📡 **实时进度** — SSE 推送分离进度，前端实时展示
- 🔍 **音频搜索** — 已分离音频模糊搜索

## 功能一览

| 功能 | 说明 |
|------|------|
| 🔍 搜索 | AudioJungle / 网易云 / QQ音乐 / 酷狗 多站点搜索 |
| 📥 下载 | 并发下载预览音频，断点续传 |
| 🎵 分离 | Meta Demucs v4 AI 模型，本地运行，无限使用 |
| 🌐 Web UI | 搜索 → 下载 → 分离 → 导出 全流程可视化 |
| 📊 进度 | SQLite 数据库跟踪状态，随时中断恢复 |
| 📤 上传 | 本地音频/视频上传，自动提取音频 |

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python | 3.10+ |
| **FFmpeg** | **必须！** Demucs 依赖 + 视频提取音频 + 格式转换 |
| Demucs v4 | `pip install demucs` |

### 安装

```bash
# 1. 安装 FFmpeg（必须！）
# Windows:
winget install Gyan.FFmpeg
# macOS:
# brew install ffmpeg
# Linux:
# sudo apt install ffmpeg

# 2. 安装 Python 依赖
pip install -r requirements.txt
```

## 快速开始

### Web 界面（推荐）

```bash
python app.py
```

浏览器自动打开 `http://localhost:8763`，所有操作在 Web UI 完成：
1. 左侧选择音乐站点 → 搜索
2. 勾选结果 → 加入队列 → 下载
3. 选中已下载 → 人声分离
4. 分离完成 → 复制/下载伴奏或人声

### 命令行

```bash
# 试用 — 下载 20 首并分离
python main.py run --preset small

# 日常 — 下载 100 首并分离
python main.py run --preset medium

# 自定义
python main.py run --max-items 50 --price-max 15

# 仅搜索（不下载）
python main.py search --price-max 19 --sort date

# 断点续传
python main.py resume

# 查看进度
python main.py status

# 逐首处理（可中断续传）
python separate_one.py
```

## 国内音乐平台

### Cookie 登录（推荐）

在 Web UI 设置页面粘贴浏览器 Cookie：
- 网易云：`MUSIC_U` 
- QQ音乐：`uin` 或 `qqmusic_key`
- 酷狗：`kg_mid` 或 `kg_mid_v2`

支持直接粘贴完整 Cookie 字符串，自动解析。

### 账号密码登录

Web UI 设置页面直接输入手机号+密码登录（网易云支持 weapi 加密）。

> ⚠️ 免费账号只能下载试听片段，VIP 账号可下载完整歌曲。

## 配置

编辑 `config.yaml`：

```yaml
search:
  category: music         # 分类
  sort: date             # 排序: date|sales|rating|price
  price_max: 19          # 最高价 (美元)

download:
  concurrency: 5         # 并发下载数

separation:
  local:
    model: htdemucs      # htdemucs(快,推荐) | htdemucs_ft(慢,质量略高)
    device: auto         # auto|cpu|cuda|directml
    jobs: 4              # 并行任务数

server:
  port: 8763
  host: 0.0.0.0
```

## 输出结构

```
downloads/previews/          # 下载的预览音频
  └── 123456_Track-Title/
      ├── preview.mp3
      └── metadata.json

separated/                   # 分离结果
  └── 123456_Track-Title/
      ├── instrumental.wav   # 伴奏（无人声）
      ├── vocals.wav         # 人声
      └── metadata.json

uploads/                     # 本地上传文件
```

## 硬件性能

基于 100 秒音频 (44.1kHz MP3) 实测：

| 设备 | 模型 | 单首耗时 |
|------|------|----------|
| CPU 16 核 (htdemucs + `-j 4`) | htdemucs (2 子模型) | ~107 秒 |
| CPU 16 核 (htdemucs_ft 无并行) | htdemucs_ft (4 子模型) | ~300 秒 |
| NVIDIA GPU (CUDA) | 任意 | ~30 秒 |

首次运行会自动下载 AI 模型（约 80MB），仅需一次。

## 项目结构

```
├── app.py                   # Web 服务入口 (FastAPI)
├── main.py                  # CLI 入口 (typer)
├── separate_one.py          # 单首分离脚本（断点续传）
├── config.yaml              # 配置文件
├── requirements.txt         # Python 依赖
├── static/
│   └── index.html           # Web 前端 (SPA)
├── src/
│   ├── config.py            # Pydantic 配置模型
│   ├── db.py                # SQLite 进度数据库
│   ├── downloader/
│   │   ├── client.py        # AudioJungle HTTP 客户端
│   │   ├── chinese_sites.py # 网易云/QQ/酷狗爬虫
│   │   ├── models.py        # 数据模型
│   │   └── filters.py       # 搜索过滤器
│   ├── separator/
│   │   └── local_engine.py  # Demucs 分离引擎
│   └── utils/
│       └── retry.py         # 重试工具
├── downloads/               # 下载缓存 (gitignore)
├── separated/               # 分离输出 (gitignore)
└── uploads/                 # 上传文件 (gitignore)
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sites` | 站点列表 + 连接状态 |
| POST | `/api/search` | 搜索音频 |
| GET | `/api/queue` | 待处理队列 |
| POST | `/api/queue/add` | 加入队列 |
| POST | `/api/download` | 下载选中项目 |
| POST | `/api/separate` | 启动人声分离 |
| GET | `/api/progress/stream` | SSE 实时进度 |
| GET | `/api/files` | 文件列表 |
| GET | `/api/files/{id}/download` | 下载/转换音频 |
| GET | `/api/search-audio` | 模糊搜索已分离音频 |
| POST | `/api/upload` | 上传本地文件 |
| POST | `/api/login/netease` | 网易云登录 |
| POST | `/api/login/qqmusic` | QQ音乐登录 |
| POST | `/api/login/kugou` | 酷狗登录 |
| POST | `/api/cookies` | 保存/验证 Cookie |

## 注意事项

- AudioJungle 预览音频是公开可访问的，本工具仅供个人学习研究
- 请遵守各平台的服务条款
- 人声分离质量取决于 AI 模型和原音频复杂度
- 分离后的 WAV 文件体积较大（约为 MP3 源的 10 倍），注意磁盘空间
- 国内平台免费用户只能下载试听片段，VIP 登录后可获取完整歌曲

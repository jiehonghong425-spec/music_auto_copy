# AudioJungle 预览音频批量下载 + 人声分离工具

从 [AudioJungle](https://audiojungle.net/) 批量下载音乐预览音频，使用 Meta Demucs v4 本地 AI 模型自动分离人声与伴奏。

## 功能

- 🔍 **搜索**: 自动翻页搜索 AudioJungle，支持价格/日期/分类过滤
- 📥 **下载**: 并发下载预览 MP3，支持断点续传
- 🎵 **分离**: 本地 AI 模型 (Demucs v4) 分离人声与伴奏，无需联网，无限使用
- 📊 **进度**: SQLite 数据库跟踪状态，随时中断续传
- 🎛️ **预设**: 3 档下载量预设 (small/medium/large)，也可手动自定义

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python | 3.10+ |
| **FFmpeg** | **必须！** Demucs 依赖 FFmpeg 解码音频 |
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

# 3. 验证
python main.py --help
```

## 快速开始

```bash
# 试用 — 下载 20 首并分离
python main.py run --preset small

# 日常 — 下载 100 首并分离
python main.py run --preset medium

# 自定义
python main.py run --max-items 50 --price-max 15

# 仅搜索（不下载）
python main.py search --price-max 19 --sort date

# 仅下载
python main.py download --concurrency 10

# 仅分离（对已下载的文件）
python main.py separate

# 断点续传
python main.py resume

# 查看进度
python main.py status

# 逐首处理（可中断续传，适合后台批量跑）
python separate_one.py
```

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
├── main.py                 # CLI 入口 (typer)
├── separate_one.py         # 单首分离脚本（断点续传）
├── config.yaml             # 配置文件
├── requirements.txt        # Python 依赖
├── src/
│   ├── config.py           # Pydantic 配置模型
│   ├── db.py               # SQLite 进度数据库
│   ├── downloader/
│   │   ├── client.py       # AudioJungle HTTP 客户端
│   │   ├── models.py       # 数据模型
│   │   └── filters.py      # 搜索过滤器
│   ├── separator/
│   │   └── local_engine.py # Demucs 分离引擎
│   └── utils/
│       └── retry.py        # 重试工具
└── downloads/              # 下载缓存 (gitignore)
└── separated/              # 分离输出 (gitignore)
```

## 注意事项

- AudioJungle 预览音频是公开可访问的，本工具仅供个人学习研究
- 请遵守 AudioJungle/Envato 的服务条款
- 人声分离质量取决于 AI 模型和原音频复杂度
- 分离后的 WAV 文件体积较大（约为 MP3 源的 10 倍），注意磁盘空间

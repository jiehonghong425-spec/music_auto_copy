# 音乐处理平台 — 手写学习指南

从头手写本项目，理解每个文件的职责和实现细节。

## 项目架构

```
config.yaml          ← YAML 配置文件
app.py               ← FastAPI Web 后端（路由 + 业务编排）
static/index.html    ← 前端（纯 HTML/CSS/JS）
src/
  config.py          ← Pydantic 配置模型
  db.py              ← SQLite 数据库层
  downloader/
    chinese_sites.py ← 网易云/QQ音乐/酷狗爬虫
    client.py        ← AudioJungle 爬虫（可选）
    models.py        ← 数据类
    filters.py       ← 筛选辅助
  separator/
    local_engine.py  ← Demucs 人声分离引擎
  utils/
    browser_cookie.py← 浏览器 Cookie 读取（可选）
    retry.py         ← 重试装饰器
```

---

## 学习顺序（6 步）

### 第一步：`src/config.py`

**为什么先写**：整个项目的骨架，所有模块都依赖配置。

**需要掌握的知识点**：

| 知识点 | 对应代码 |
|--------|---------|
| Pydantic BaseModel | `class Config(BaseModel)` |
| 嵌套模型 | `CookiesConfig` 嵌套在 `Config` 中 |
| field_validator | `@field_validator("netease", mode="before")` 做旧格式兼容 |
| YAML 读写 | `yaml.safe_load()` / `yaml.dump()` |
| Path 类型处理 | `output_dir: Path = Path("./separated")` |
| Literal 联合类型 | `sort: Literal["date", "sales", "rating", "price"]` |

**写完后验证**：
```bash
python -c "
from src.config import load_config
c = load_config('./config.yaml')
print(c.server.port, c.download.concurrency)
"
```

---

### 第二步：`src/db.py`

**为什么第二**：数据存储层，独立于业务逻辑。学会建表和 CRUD。

**需要掌握的知识点**：

| 知识点 | 对应代码 |
|--------|---------|
| sqlite3 连接 | `sqlite3.connect(db_path)` |
| 建表语句 | `CREATE TABLE IF NOT EXISTS items (...)` |
| 参数化查询 | `conn.execute("SELECT ... WHERE id = ?", (id,))` |
| Row 工厂 | `conn.row_factory = sqlite3.Row`（返回字典式访问） |
| INSERT OR IGNORE | 防止重复插入 |
| 状态机 | `pending → downloading → downloaded → separating → separated` |

**写完后验证**：
```bash
python -c "
from src.db import Database
db = Database(':memory:')  # 内存数据库测试
db.conn.execute('SELECT 1')
print('OK')
"
```

---

### 第三步：`src/downloader/chinese_sites.py`

**核心逻辑** — 三个爬虫类，搜索歌曲 + 获取下载链接。

**需要掌握的知识点**：

| 知识点 | 对应代码 |
|--------|---------|
| httpx.AsyncClient | 异步 HTTP 请求 |
| asyncio.gather | 并行发起多个请求 |
| 多策略兜底 | `get_download_url()` 依次尝试不同 API |
| Cookie 注入 | `_merge_cookies()` 将 dict 转为 Cookie 头 |
| 工厂模式 | `SCRAPER_MAP` + `get_scraper()` |
| 搜索结果结构化 | 统一返回 `{id, title, author, source, ...}` |

**三个平台的 API**：

| 平台 | 搜索 API | 下载 API |
|------|---------|---------|
| 网易云 | `music.163.com/api/search/get` | `music.163.com/api/song/enhance/player/url` + outer URL 兜底 |
| QQ音乐 | `c.y.qq.com/soso/fcgi-bin/client_search_cp` | `u.y.qq.com/cgi-bin/musicu.fcg` (CgiGetVkey) |
| 酷狗 | `mobilecdn.kugou.com/api/v3/search/song` | `m.kugou.com/app/i/getSongInfo.php` (playInfo) |

**写完后验证**：
```python
import asyncio, httpx
from src.downloader.chinese_sites import get_scraper

async def test():
    async with httpx.AsyncClient(timeout=15) as cli:
        s = get_scraper("netease", cli)
        items = await s.search("晴天", 10)
        for i in items[:3]:
            print(i["title"], "—", i["author"])

asyncio.run(test())
```

---

### 第四步：`src/separator/local_engine.py`

**人声分离** — 调用 Meta Demucs CLI。

**需要掌握的知识点**：

| 知识点 | 对应代码 |
|--------|---------|
| subprocess.run | 调用 demucs 命令行 |
| asyncio.run_in_executor | 在异步中执行同步阻塞调用 |
| shutil.copy2 / move | 文件复制和移动 |
| Path 遍历 | 从 demucs 临时输出目录找到 vocals.wav / no_vocals.wav |
| 兜底检测 | `_find_ffmpeg()` 遍历多个可能路径 |
| 设备检测 | `_detect_device()` 按 cuda > directml > cpu 顺序 |

**写完后验证**（需要安装 demucs 和 ffmpeg）：
```bash
python -c "
from src.separator.local_engine import LocalSeparationEngine
e = LocalSeparationEngine(...)
e._init_separator()
print('引擎就绪')
"
```

---

### 第五步：`app.py`

**Web 后端** — 把所有模块串起来，提供 REST API。

**需要掌握的知识点**：

| 知识点 | 对应代码 |
|--------|---------|
| FastAPI 路由 | `@app.get("/api/...")` / `@app.post(...)` |
| lifespan | `@asynccontextmanager` 管理启动/关闭 |
| Pydantic 请求体 | `class SearchRequest(BaseModel)` |
| 异步并行 | `asyncio.gather()` 同时搜三个平台 |
| SSE 推送 | `StreamingResponse` + `text/event-stream` |
| 文件下载 | `FileResponse` |
| 文件上传 | `UploadFile` + `aiofiles` |
| 后台任务 | `asyncio.create_task()` 跑人声分离 |
| 匹配度评分 | `_score()` 对歌名和歌手打分，过滤 < 20 分结果 |
| HTTPException | 统一错误处理 |

**API 路由一览**：

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/config` | 读取配置 |
| PUT | `/api/config` | 保存配置 |
| GET | `/api/sites` | 站点列表 |
| POST | `/api/sites` | 添加站点 |
| DELETE | `/api/sites/{id}` | 删除站点 |
| POST | `/api/sites/refresh` | 刷新站点状态 |
| POST | `/api/search` | 搜索音乐（同时搜三平台） |
| GET | `/api/queue` | 待处理队列 |
| POST | `/api/queue/add` | 加入队列 |
| DELETE | `/api/queue/{id}` | 移出队列 |
| POST | `/api/download` | 下载 |
| POST | `/api/separate` | 开始分离 |
| POST | `/api/separate/stop` | 停止分离 |
| GET | `/api/progress/stream` | SSE 进度推送 |
| GET | `/api/files` | 文件列表 |
| GET | `/api/files/{id}` | 文件详情 |
| GET | `/api/files/{id}/download` | 下载/转换音频 |
| GET | `/api/search-audio` | 搜索本地已分离音频 |
| POST | `/api/upload` | 上传音频/视频 |

**匹配度评分算法**：

```
关键词 = "晴天 周杰伦" (小写，按空格分词)

评分规则（对每首歌）：
  歌名完全匹配关键词                   +100
  歌名以关键词开头                     +80
  歌名包含关键词（词边界）             +60
  歌名包含关键词（子串）               +40
  歌手完全匹配                         +30
  歌手以关键词开头                     +25
  歌手包含关键词                       +15
  歌名词逐一匹配（完全相等）           每个 +10
  歌名词逐一匹配（前缀，≥3字）         每个 +5
  歌手词逐一匹配                      每个 +3~8

结果：按总分降序，< 20 分的结果丢弃
```

**写完后验证**：
```bash
python app.py
# 浏览器打开 http://localhost:8763
```

---

### 第六步：`static/index.html`

**前端** — 纯 HTML/CSS/JS，零框架依赖。

**页面结构**：

```
侧边栏（仪表盘 | 音频搜索 | 文件管理 | 上传 | 设置）
├── 仪表盘
│   ├── 统计卡片（待处理/已下载/已分离/失败）
│   ├── Tab 切换列表
│   ├── 进度条 + 分离控制
│   └── 项目列表（带操作按钮）
├── 音频搜索
│   ├── 搜索框 + 按钮
│   ├── 搜索结果（▶ 试听 + 加入队列）
│   └── 站点管理（增删查）
├── 文件管理
│   ├── 状态筛选
│   └── 文件表格（▶ 播放 + 伴奏/人声下载）
├── 上传
│   └── 拖拽区域 + 文件选择 + 格式提示
└── 设置
    └── 模型/线程/并发/设备/输出目录
```

**需要掌握的知识点**：

| 知识点 | 对应代码 |
|--------|---------|
| fetch API | `async function api(method, url, body)` |
| DOM 操作 | `document.getElementById()` / `querySelector()` |
| innerHTML 模板 | 动态生成列表/表格 |
| EventSource (SSE) | `new EventSource('/api/progress/stream')` |
| HTML5 Audio | `<audio>` 标签 + `play()` / `pause()` |
| FileReader / FormData | 文件上传 |
| CSS 变量 | `var(--bg)` / `var(--accent)` |
| Flexbox / Grid | 布局 |
| CSS 动画 | `@keyframes fade` |

**写完后验证**：启动 `python app.py`，浏览器访问，依次验证每个页面功能。

---

## 可以跳过不写的文件

| 文件 | 理由 |
|------|------|
| `main.py` | CLI 入口，和 app.py 功能重复，学完 app.py 自然能写 |
| `src/downloader/client.py` | AudioJungle 爬虫，逻辑和第三步相似 |
| `src/downloader/models.py` | 简单数据类，无学习价值 |
| `src/downloader/filters.py` | 辅助过滤函数 |
| `src/utils/browser_cookie.py` | 浏览器 Cookie 读取，可选模块 |
| `src/utils/retry.py` | 重试装饰器，通用工具 |

---

## 写的过程中如何验证

```bash
# 每个文件写完后
python -c "import py_compile; py_compile.compile('文件.py', doraise=True)"

# 第三步写完
python -c "
import asyncio, httpx
from src.downloader.chinese_sites import get_scraper
async def t():
    async with httpx.AsyncClient() as c:
        s = get_scraper('netease', c)
        print(await s.search('晴天', 3))
asyncio.run(t())
"

# 第五步写完
python app.py
# 浏览器 http://localhost:8763/docs 查看 Swagger API 文档
```

---

## 关键设计模式总结

| 模式 | 出现位置 | 作用 |
|------|---------|------|
| 工厂模式 | `chinese_sites.py` 的 `SCRAPER_MAP` | 按字符串创建不同爬虫 |
| 多策略兜底 | `get_download_url()` | 依次尝试多个 API 直到成功 |
| 状态机 | `db.py` 的 items 状态流转 | pending→downloading→downloaded→separating→separated |
| 异步并行 | `app.py` 的 `asyncio.gather` | 同时搜三个平台 |
| SSE 推送 | `app.py` + `index.html` 的 EventSource | 实时进度推送 |
| 评分排序 | `app.py` 的 `_score()` | 匹配度排序 + 低分过滤 |

# 教培海报自动化工作台 · MVP

本地运行的人像素材流水线：**rembg 去背景 → 人脸检测 → 智能头肩裁切 → 海报尺寸 PNG**。
老师照片只在本机 CPU 上处理，不上传任何服务器，适合教培机构批量出海报素材。

技术栈：FastAPI + 原生 HTML / Tailwind（CDN）/ Alpine.js，图像处理用 OpenCV + rembg + MediaPipe / YuNet。

---

## 1. 功能

- **本地抠图**：rembg（u2net / isnet 等 ONNX 模型）输出 RGBA 透明底人像。
- **人脸检测**：OpenCV YuNet → MediaPipe → OpenCV Haar 三级自动降级，全程离线。
- **三种人像版式**：`head_shoulder` 半身头肩、`framed_circle` 圆框头像（抗锯齿 + 内侧描边）、`full_body` 全身立绘（按 alpha 外轮廓裁切）。
- **智能头肩裁切**：以人脸为中心，向上留 `0.45h`、向下延伸 `0.75h`；全身照自动裁掉腰部以下；未检测到人脸时退化为顶部居中裁剪。
- **精确出图**：严格缩放到目标尺寸（1080×1440、1:1、9:16…），预乘 alpha 缩放，边缘不发黑。
- **可视化工作台**：拖拽上传、版式切换、比例预设、构图/描边滑杆、原图人脸/裁切框叠加、成品下载。
- **海报模板自动解析与合成**：上传设计师导出的 SVG，大模型（可离线降级）推断字段与人像 mode，粘贴排课文案 + 老师照片即可套版出高清 PNG。
- **可调试接口**：`analyze` 只检测不落盘，`process` 返回人脸框、裁切框、耗时和告警。

## 2. 目录结构

```
.
├─ app/
│  ├─ main.py                  # FastAPI 入口、异常处理、静态页面
│  ├─ config.py                # 环境变量配置
│  ├─ errors.py                # 领域异常 → HTTP 状态码
│  ├─ schemas.py               # Pydantic 出参模型
│  ├─ api/
│  │  └─ images.py             # /api/v1/images/* 接口
│  └─ services/
│     ├─ image_processor.py    # ★ 核心：解码/抠图/几何/缩放/裁切
│     ├─ face_detector.py      # MediaPipe / YuNet / Haar 检测器
│     ├─ pipeline.py           # 抠图 → 检测 → 裁切 流水线
│     ├─ template_analyzer.py  # SVG 模板解析 → schema.json
│     ├─ template_renderer.py  # 文案解析 + 套版 + Playwright 导出
│     ├─ svg_browser.py        # 无头浏览器：量尺寸/缩字号/截图
│     └─ llm_client.py         # OpenAI 兼容多模态客户端
├─ static/
│  ├─ index.html               # 人像素材工作台
│  └─ poster.html              # 海报模板工作台
├─ scripts/
│  ├─ batch_process.py         # 批量处理一个文件夹
│  ├─ analyze_template.py      # 命令行解析 SVG 模板
│  └─ fetch_models.py          # 下载 YuNet / MediaPipe / Haar 模型
├─ tests/
│  ├─ conftest.py
│  ├─ test_crop_geometry.py    # 裁切几何（不依赖任何模型）
│  ├─ test_image_processor.py  # 编解码 / 预乘缩放 / rembg 适配
│  └─ test_api.py              # 接口集成测试
├─ data/                       # 运行时生成：outputs/ 成品，models/ 模型
├─ requirements.txt
├─ requirements-dev.txt
├─ pytest.ini
└─ README.md
```

## 3. 环境要求

| 项 | 建议 |
|---|---|
| Python | 3.10 – 3.12（mediapipe 尚无 3.13 wheel） |
| 系统 | Windows / macOS / Linux，纯 CPU 即可 |
| 内存 | 抠图峰值约 1.5 GB |
| 磁盘 | 依赖约 300 MB + rembg 模型约 170 MB |

## 4. 安装

```powershell
# 1) 创建虚拟环境
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate

# 2) 安装依赖（国内网络可加清华镜像）
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3) 可选：下载 YuNet / MediaPipe / Haar 人脸模型（每个约 230 KB）
python -m scripts.fetch_models

# 4) 海报套版需要无头浏览器：默认复用本机 Edge，无需额外下载
#    若机器没有 Edge：python -m playwright install chromium
```

> 海报模板解析/合成还依赖 `lxml`、`openai`、`playwright`（已包含在 requirements.txt）。
> 转曲文字复活额外依赖 `rapidocr-onnxruntime`（本地 CPU OCR，无需 API Key）；未安装或模型
> 不可用时上传仍成功，只在 warnings 中提示跳过。

> `requirements.txt` 同时安装了 `opencv-python-headless`（rembg 需要）和 mediapipe 依赖的
> `opencv-contrib-python`。两者都提供 `cv2`，后装者生效，功能等价，属正常现象。

**首次抠图**会自动下载 u2net 模型（约 170 MB）到 `POSTER_CACHE_DIR/rembg`
（默认 `D:\DSH\.poster-cache\rembg`，可用 `U2NET_HOME` 覆盖），之后完全离线。
如果网络受限，可手动下载 `u2net.onnx` 放进该目录。

## 5. 启动

```powershell
uvicorn app.main:app --reload --port 8000
```

- 工作台页面：<http://127.0.0.1:8000>
- 接口文档：<http://127.0.0.1:8000/docs>

命令行批量处理：

```powershell
python -m scripts.batch_process 老师照片 --out samples --width 1080 --height 1440
```

## 6. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/images/analyze` | 只检测人脸，返回建议裁切框，不落盘 |
| `POST` | `/api/v1/images/cutout` | 只去背景，返回透明底 PNG 信息 |
| `POST` | `/api/v1/images/crop` | 只做头肩裁切（默认不抠图） |
| `POST` | `/api/v1/images/process` | 完整链路：抠图 + 检测 + 裁切 |
| `GET` | `/api/v1/images/files/{filename}` | 读取生成的 PNG |
| `GET` | `/api/v1/health` | 健康检查（rembg 是否可用等） |

`/process` 与 `/crop` 的表单参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `file` | 必填 | JPG / PNG / WebP / BMP，≤ 20 MB |
| `mode` | `head_shoulder` | 版式：`head_shoulder` / `framed_circle` / `full_body` |
| `target_width` / `target_height` | 1080 / 1440 | 成品尺寸，64–4096；圆框模式按短边输出正方形 |
| `top_ratio` | 按 mode | 头顶留白 = `top_ratio × 人脸框高`（头肩 0.45，圆框 0.50） |
| `shoulder_ratio` | 按 mode | 下巴向下延伸（头肩 0.75，圆框 0.85） |
| `remove_bg` | 按 mode | 头肩：`/process` 为 true、`/crop` 为 false；圆框默认 false；全身强制 true |
| `border_width` | `0` | 圆框内侧描边宽度（px，0–512） |
| `border_color` | `#FFFFFF` | 圆框描边颜色 `#RRGGBB` |
| `face_x/y/w/h` | 空 | 手动指定人脸框，绕过检测（四个一起传） |

```bash
# 半身头肩（默认）
curl -X POST http://127.0.0.1:8000/api/v1/images/process \
  -F "file=@老师照片/1.曾芳芳照片.jpg" \
  -F "target_width=1080" -F "target_height=1440"

# 圆框头像：正方形 + 12px 白色描边
curl -X POST http://127.0.0.1:8000/api/v1/images/process \
  -F "file=@老师照片/7.方芬照片.jpg" \
  -F "mode=framed_circle" -F "target_width=1080" -F "target_height=1080" \
  -F "border_width=12" -F "border_color=#FFFFFF"

# 全身立绘：强制抠图，按 alpha 外轮廓裁切（首次运行需要 u2net 模型）
curl -X POST http://127.0.0.1:8000/api/v1/images/process \
  -F "file=@老师照片/3.林晓娟照片.jpg" \
  -F "mode=full_body" -F "target_width=1080" -F "target_height=1440"
```

响应示例（节选）：

```json
{
  "request_id": "6f1c0a9d2b34",
  "source": { "width": 1279, "height": 1751 },
  "target": { "width": 1080, "height": 1440 },
  "output": {
    "url": "/api/v1/images/files/6f1c0a9d2b34_poster.png",
    "width": 1080, "height": 1440, "bytes": 483120
  },
  "cutout": { "url": "/api/v1/images/files/6f1c0a9d2b34_cutout.png", "...": "..." },
  "face": { "x": 449, "y": 409, "w": 176, "h": 234, "confidence": 0.93, "backend": "opencv-yunet" },
  "crop": { "left": 344, "top": 304, "width": 386, "height": 515 },
  "strategy": "face",
  "mode": "head_shoulder",
  "warnings": [],
  "timings": { "decode_ms": 18.2, "detect_ms": 26.5, "cutout_ms": 1840.7, "crop_ms": 9.4, "total_ms": 1901.1 }
}
```

## 7. 海报模板自动解析与合成

### 7.1 工作流

```
设计师 SVG ──解析──> schema.json（字段 / 坐标 / 人像 mode）
排课通知文本 ──LLM/规则──> {节点ID: 新文字}
老师照片 ──pipeline（按 mode）──> 透明底 PNG ──Base64──> <image>（保留 clipPath）
                                     ↓
                       Playwright deviceScaleFactor=2 → 高清 PNG
```

1. **模板解析**：`lxml` 提取 `<text>` / `<tspan>` / `<image>` 与坐标；缺失的图层自动补
   `text-001` / `tspan-001` / `image-001` / `clip-001` 稳定 ID；坐标 <5px 且内容相似度 ≥90%
   的多余文字图层会被判为重复层并移除（保留最上层），避免替换后重影。无头浏览器量出真实
   包围盒（`<text>` 的直接文字用 Range 只量这一段）。再让多模态大模型推断业务字段与人像
   mode，结果落在 `data/templates/<name>.schema.json`，原文件保持
   `data/templates/<name>.fixed.svg`。
2. **文案解析**：把整段排课通知交给大模型，返回 `{节点ID: 新文字}`；没配模型时用规则解析
   （首行=主标题、含“丨”=课程、日期正则=时间、二维码关键词=引导语）。
3. **素材准备**：老师照片在上传时就走 `pipeline.run_pipeline`（按坑位 mode 裁成透明 PNG），
   二维码做等比压缩，结果存进 `data/assets/teachers` / `data/assets/qrcodes`，同时返回缩略图。
4. **套版与导出**：预览只做 SVG 文本替换 + Base64 嵌图，不启动浏览器；导出时再测量宽度、
   缩字号（不低于 85%），Playwright 以 `device_scale_factor=2` 输出
   `data/outputs/posters/<job_id>/poster.png` 与 `poster.svg`。

### 7.2 使用

工作台：<http://127.0.0.1:8000/static/poster.html>

模板直接从 `data/templates/*.fixed.svg` 读取，首次点开时自动生成缩略图与 schema；
也可以在「切换模板」弹窗里点「上传 SVG / AI / EPS」或虚线卡片（支持点击选择 / 拖拽）。
`.ai` / `.eps` 会先用 Ghostscript + poppler（`pdftocairo`）转换第 1 页为 SVG，再自动清洗、
生成 320px 缩略图并尝试把转曲文字复活成可编辑 `<text>`；同名文件自动加 `-2` 后缀。
AI/EPS 上传默认不设体积上限；部署时可配置 `MAX_TEMPLATE_UPLOAD_MB`（`0` 表示不限制）。上传按 2MB 分块落盘，灵动岛展示进度；转换时超大位图按显示尺寸降采样并外置到模板资源目录，`.fixed.svg` 保持轻量。磁盘空间需足以暂存源文件、Ghostscript PDF 与资源文件。模板卡片悬停右上角会出现删除按钮，
点击后弹出 Apple 风格确认框，确认后才会删除模板及解析缓存。
左侧三步完成「AI 填表 → 文案微调 → 师资/二维码」，右侧实时预览，右上角导出高清 PNG。

命令行解析模板（不经过工作台也可以）：

```powershell
python -m scripts.analyze_template "电视台红色海报多人_复制-01.fixed.svg" --no-llm
```

配置大模型（任意 OpenAI 兼容服务，支持视觉时传模板缩略图）：

```powershell
$env:LLM_API_KEY="sk-..."
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="gpt-4o-mini"
# 或写进项目根目录 .env（也兼容 OPENAI_API_KEY / OPENAI_BASE_URL）
```

AI/EPS 转换工具（不配置时会尝试 `D:\PosterTools` 和项目内 `data/installers`）：

```ini
PDFTOCAIRO_BIN=D:\PosterTools\poppler-26.09.0\Library\bin\pdftocairo.exe
GHOSTSCRIPT_BIN=D:\PosterTools\gs\bin\gswin64c.exe
MAX_VECTOR_UPLOAD_MB=200
```

未配置或调用失败时自动降级为规则解析，接口返回 `engine="heuristic"` 并在 `warnings` 中说明。

### 7.3 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/posters/templates/upload` | 上传 `.svg/.ai/.eps`：AI/EPS 自动转 SVG、清洗 + 320px 缩略图；`analyze=true` 可顺带解析 |
| `GET` | `/api/v1/posters/templates` | 扫描 `*.fixed.svg`，返回 name / display_name / thumbnail_url / analyzed |
| `GET` | `/api/v1/posters/templates/{name}/thumbnail` | 320px 宽 JPEG；首次用 Playwright 渲染并缓存 |
| `POST` | `/api/v1/posters/templates/{name}/analyze?force=true` | 解析模板并写 `<name>.schema.json` |
| `GET` | `/api/v1/posters/templates/{name}/schema` | 读取 schema |
| `DELETE` | `/api/v1/posters/templates/{name}` | 删除模板源文件、schema、预览 SVG、缩略图与复活备份 |
| `POST` | `/api/v1/posters/parse-text` | `{template_name, raw_text}` → `{values, engine, warnings}` |
| `POST` | `/api/v1/posters/preview` | `{template_name, data, images}` → 纯 SVG（无浏览器） |
| `POST` | `/api/v1/posters/export` | 高清 PNG，返回 `url` / `filename` / 尺寸 |
| `GET` | `/api/v1/posters/files/{job}/{file}` | 读取 `poster.png` / `poster.svg` |
| `POST` | `/api/v1/posters/assets/upload` | 上传并处理头像/二维码，自动入库 |
| `GET` | `/api/v1/posters/assets?kind=teacher` | 历史素材列表（含缩略图 URL） |
| `GET` | `/api/v1/posters/assets/file/{id}` | 素材原图 |
| `GET` | `/api/v1/posters/assets/thumb/{id}` | 素材缩略图 |

`images` 的 key 是 schema 里的坑位 key（`portrait_1` / `portrait_2` / `qr_frame`），
value 支持素材 URL、素材 ID、`data:image/...;base64,...` 或裸 Base64。

### 7.4 实现细节

- **模板与缩略图**：模板以 `data/templates/<name>.fixed.svg` 为准，解析产物是同名的
  `.template.svg` 与 `.schema.json`；画廊缩略图懒生成到 `data/templates/thumbs/<name>.jpg`。
  上传时会先跑 `svg_sanitizer`：修复 `font-family` 内嵌双引号、转义孤立 `&`、按
  `width/height` 与 `viewBox` 互相补全，再落盘并生成缩略图。
- **素材库**：上传的老师照片按坑位 mode 处理后写入 `data/assets/teachers`，二维码写入
  `data/assets/qrcodes`，索引在 `data/assets/index.json`；重复图片按内容哈希去重。
- **预览与导出**：`/preview` 只做 lxml 替换 + Base64 嵌图，毫秒级返回 SVG；前端把 SVG
  内联到画布并按 `max_width` 做一次浏览器端字号收敛。画布编辑后导出走 `/export-svg`，
  直接把当前 SVG DOM 序列化交给 Playwright，以 `device_scale_factor=2` 出图，避免重新套版
  丢失拖拽、局部 tspan 和颜色修改；服务端会补 `xmlns` / `xmlns:xlink` 防止临时文件白屏。
- **蒙版**：人像框优先取 `<image>` 外层 `clip-path` 对应 `clipPath` 图形的真实外接框，而不是
  和旧 `<image>` 包围盒做交集；替换图片时移除旧 `transform`，按父级 CTM 反算到局部坐标后
  把 `x/y/width/height` 铺满蒙版，并强制 `preserveAspectRatio="xMidYMid slice"`，避免
  Illustrator 旧缩放矩阵导致头像局部放大。前端替换与 Playwright 导出使用同一套坐标规则。
- **字号**：`max_width` 取原文字实测宽度；若后面还有同一行的 tspan，则按它的 `x` 截断，
  避免替换后压到副标题；缩小到 85% 仍溢出时给出 warning。
- **重影与残留**：同坐标重复文字层在解析阶段移除；替换整段 `<text>` 时先清空其所有
  `<tspan>` 再写入新文本；若该 `<text>` 下还有别的独立字段（如副标题 tspan），则只替换
  对应片段、保留兄弟节点。
- **行首单字拆分**：Illustrator 有时把“扫”拆成独立节点，替换时若新文本首字与左侧紧邻
  单字相同（同基线且间距 <8px），会清空/移除该单字并把整行左移回原行首，避免“扫扫码”。
- **颜色保留**：内容未改的字段原样保留逐字 tspan（含多色）不动；确实替换时按原文字片段
  的颜色重建，`【...】` 自动用原高亮色包裹，避免白底白字。
- **同行碎片合并**：同一基线（y 差 <8px）、字号差 ≤3、颜色/描边相同的相邻 `<text>`，
  间距小于 1.5 倍字号时合并成一个字段（例如“4”+“月11日”→“4月11日”），渲染时写入
  首节点并清掉同组碎片。
- **画板所见即所得**：引入本地 `moveable` 控制框。单击文字/图片进入对象选中态，可拖拽、
  等比缩放并显示画布中心/边缘吸附参考线；缩放结束把比例换算成 `font-size`，不会留下
  `scale()` 矩阵拉伸文字。双击文字进入原位编辑：不再弹白色输入框，只显示细虚线框，
  通过屏幕上的透明 textarea 代理接收键盘/IME，输入时实时写回 SVG `textContent`，因此能
  直接看到真实字体、字重、渐变和居中效果；Esc 取消、Enter 提交，点击其他元素会先提交
  锁定目标再切换，避免 blur 串台。多字号同行按 tspan 独立编辑，不覆盖兄弟 tspan。
  右侧属性面板可改字体、字号、字重、字间距、行高、填充色，划选部分文字时通过拆分
  `<tspan>` 做局部样式。`Ctrl/Cmd+Z` / `Ctrl/Cmd+Shift+Z` 基于 SVG DOM 快照撤销重做。
- **图片替换**：点头像/二维码选中后，右侧面板「替换图片」复用素材上传接口；`<image>`
  的 `href` 始终写服务端可访问的 `/api/v1/posters/assets/...` 地址，不使用 `blob:`，
  保证 `/export-svg` 的 Playwright 能加载图片。
- **大模型状态**：`/api/v1/health` 返回 `llm.configured/model`，顶部状态胶囊显示
  「AI 已连接 / 规则模式」；`.env` 固定从项目根目录加载。
- **本地字体**：把 `.ttf` / `.otf` 放进 `static/Fonts`，解析时读取 family 与 PostScript
  名，自动生成 `@font-face`。除了 3.0 的 `AlibabaPuHuiTi_3_105_Heavy-GBpc-EUC-H`，也兼容
  老版 Illustrator 命名 `AlibabaPuHuiTiB/M/H/R/L/T/EB/BL`（映射到同字重的 3.0 文件），
  无头浏览器量尺寸、导出 PNG 都用真实字体。
- **多字号同行**：同一 `<text>` 内若包含多个不同字号或不同 `fill` 的 `<tspan>`，schema 会
  按独立 tspan 拆分（嵌套 tspan 的直接文字会先包成独立 run）；画板只编辑被点击的 tspan，
  渲染端也只改目标 tspan。前端另有兜底：schema 漏掉的可见 `<text>`/`<tspan>` 会自动分配
  `auto:` 临时字段并绑定双击编辑，确保画布上没有点不动的文字。
- **转曲文字复活**：上传或点击「重新识别坑位」时，先渲染一次预览图，用本地 RapidOCR
  识别所有视觉文字行，再与现有 `<text>` 的实测 bbox 比对；没有被活文字覆盖的行，会在
  同一区域寻找 `<path>`/`<g>` 轮廓组，提取 `fill`（含 `url(#grad-xxx)`）后删除轮廓，
  在原位置插入可编辑 `<text>`（普惠体 3.0，字高 ≥80→800、≥48→700、其余 500）。
  渐变会克隆一份 `gradientUnits="objectBoundingBox"` 的专用渐变，拖动文字时跟随文字。
  任何几何验证不通过、轮廓组不确定、带 clip/mask 的行都会跳过并写 warning，原文件不变。
  新建 `<text>` 插入到原轮廓组同级、而非带 transform 的父组内，避免坐标二次偏移。
- **字体统一**：所有字重都注册为同一个 family `'Alibaba PuHuiTi 3.0'`，仅用
  `font-weight` 区分：55→400、65→500、85→700、105→800、115→900。清洗 SVG 时按原字体名
  后缀（H/Heavy/105、Black/115、B/Bold/85、M/Medium/65、R/Regular/55）推断字重，写入
  `font-weight` 后把 family 统一替换，保证和 `@font-face` 100% 命中。
- **描边保留**：清理同坐标重复 `<text>` 前先读取底层节点的 `stroke` / `stroke-width`，
  转到保留下来的上层节点；schema 的 `style` 记录 `stroke_color` / `stroke_width`，
  渲染时补 `paint-order: stroke fill; stroke-linejoin: round`，白字外的深红描边不会丢。
- **破折号可编辑**：同行合并对纯 `—` 节点单独放宽条件（间距 <3 倍字号、允许字号差），
  会把 `——` 与后面的课题文本合成一个字段；前端给合并组内所有 `<text>` 都绑定同一个
  点击编辑事件，点破折号或课题文字都会弹出整行编辑器。
- **空值保护**：文案里没提到的字段、大模型返回 `None` / `""` 时一律保留模板默认文字。

### 7.5 已知限制

- 预览不启动浏览器，字号收敛由前端近似完成；最终像素级排版以导出 PNG 为准。
- 模板里与文字分离的装饰高亮块不会跟随文字移动（例如二维码引导语的白底）。
- `full_body` 依赖 rembg u2net 模型；模型未就绪时该人像会保留模板原图并提示。
- 大模型视觉能力取决于服务商；不支持图片时自动退回纯文本推理。
- 单张海报生成约 4–8 秒（含无头浏览器启动），首次解析模板约 10–30 秒。

## 8. 核心算法

### 7.1 头肩裁切几何

设人脸框为 `(x, y, w, h)`，目标宽高比 `aspect = target_width / target_height`：

```
裁切高度 = h × (1 + top_ratio + shoulder_ratio)     # 默认 2.2h
裁切顶部 = y − top_ratio × h                        # 默认 y − 0.45h
裁切底部 = y + h + shoulder_ratio × h               # 默认 y + 1.75h
裁切宽度 = 裁切高度 × aspect
裁切左边 = (x + w/2) − 裁切宽度 / 2                 # 以人脸中心水平对齐
```

实现见 `app/services/image_processor.py:compute_crop_box()` 与 `crop_head_and_shoulders()`。

边界处理：

1. **超出原图**：先平移贴边（保持窗口高度），例如人脸靠上时 `top = 0`。
2. **窗口比原图还大**：缩到原图尺寸，并保证目标比例。
3. **未检测到人脸**：顶部居中裁剪 —— 宽度取 `min(图宽, 图高 × aspect)`，`top = 0`。
4. **全身照**：人脸通常很小，2.2h 的窗口天然把腰部以下裁掉，然后放大到目标尺寸；
   放大超过 3 倍会在 `warnings` 里提示换更高清的原图。
5. **整数像素取整后比例有偏差**：用透明像素补齐到精确比例（`fit_aspect()`），不拉伸人脸。

### 7.2 透明边缘

缩放前把 RGB 与 alpha 预乘，在预乘空间重采样后再还原，避免半透明边缘混入黑底产生灰边
（`resize_rgba()`）。裁切超出原图或补比例时，填充的是 `(0,0,0,0)`，导到海报里不会出现白边。

### 7.3 检测后端

| 顺序 | 后端 | 依赖 | 说明 |
|---|---|---|---|
| 1 | `opencv-yunet` | OpenCV ≥ 4.5.3 + 230 KB 模型 | 人脸框贴近头部轮廓，按 0.45h 留白不切头发 |
| 2 | `mediapipe` | mediapipe + Tasks 模型 | 新版走 Tasks API，旧版走 `solutions`；框偏紧，作备选 |
| 3 | `opencv-haar` | OpenCV 4.x 自带 xml | 兜底，正脸/侧脸 |

设置 `FACE_BACKEND=mediapipe` 可以强制使用 MediaPipe。之所以默认 YuNet：MediaPipe
BlazeFace 的框只框住脸部（不含头发），按 `0.45h` 留白时容易把发顶切掉；YuNet 的框更接近
"头部"范围，构图更稳。检测统一先缩到长边 1600 px 再推理，坐标映射回原图。所有后端失败时
接口返回 503，但 `/process` 会捕获异常并退化为顶部居中裁剪，仍能出图。

> 中文目录注意：OpenCV / MediaPipe 的 C++ 加载器都不认非 ASCII 路径。代码会把模型自动
> 复制到纯英文缓存目录（`POSTER_CACHE_DIR`，默认 `D:\DSH\.poster-cache`）再加载，
> 所以项目放在 `D:\DSH\自动修改海报\` 也能正常工作。若该目录不可写，会给出明确报错。

### 7.4 三种人像版式（`mode`）

| mode | 默认抠图 | 构图 | 适用 |
|---|---|---|---|
| `head_shoulder` | `/process` 是，`/crop` 否 | 人脸框 `2.2h` 窗口，0.45 / 0.75 | 卡片半身位 |
| `framed_circle` | 否（保留背景） | 正方形，0.50 / 0.85 + 圆形蒙版 | 圆形头像 / 讲师徽标 |
| `full_body` | **强制是** | alpha 外轮廓 + 3% 边距，底部对齐 | 全身立绘、人物介绍 |

**framed_circle**：裁切比例固定为 1:1（`target_width/height` 取短边），缩放完成后调用
`apply_circle_mask()`。蒙版在 4 倍尺寸上画圆再做 `INTER_AREA` 缩回，得到抗锯齿边缘；
四角 alpha 为 0。`border_width > 0` 时在圆周**内侧**按 `border_color` 画环，描边不会
超出圆形轮廓。

**full_body**：忽略人脸框，直接对抠图结果的 `alpha > 10` 像素求最小外接矩形，四周
外扩 3% 后调用 `fit_aspect(anchor=(0.5, 1.0))` 与 `resize_rgba()`。底部锚点保证比例
不匹配时只在上方补透明像素，人物脚底始终贴近目标画布底边；没有任何 alpha 时退化为
整图并给出 warning。

## 9. 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 输出与模型目录 |
| `POSTER_CACHE_DIR` | 项目上级目录的 `.poster-cache` | 纯英文缓存目录：模型副本、matplotlib、rembg |
| `MAX_UPLOAD_MB` | `20` | 上传大小上限 |
| `MAX_OUTPUT_SIDE` | `4096` | 成品边长上限 |
| `REMBG_MODEL` | `u2net` | rembg 模型名（`u2net`、`isnet-general-use`…） |
| `REMBG_REQUIRED` | `false` | `true` 时抠图失败直接报 503，而不是退回原图 |
| `REMBG_ALPHA_MATTING` | `false` | 发丝级 alpha matting（更慢，需 pymatting） |
| `U2NET_HOME` | `POSTER_CACHE_DIR/rembg` | rembg 模型存放目录 |
| `FACE_BACKEND` | `auto` | `auto`=yunet→mediapipe→haar，也可强制指定 |
| `FACE_MIN_CONFIDENCE` | `0.5` | 人脸置信度阈值 |
| `FACE_MAX_SIDE` | `1600` | 检测前缩放长边 |
| `YUNET_MODEL` | `data/models/face_detection_yunet_2023mar.onnx` | YuNet 模型路径 |
| `MEDIAPIPE_MODEL` | `data/models/blaze_face_short_range.tflite` | MediaPipe Tasks 模型路径 |
| `HAAR_CASCADE_DIR` | `data/models/haarcascades` | Haar xml 目录 |
| `LLM_API_KEY` | 空 | OpenAI 兼容服务的 Key（兼容 `OPENAI_API_KEY`） |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | 兼容 `OPENAI_BASE_URL` |
| `LLM_MODEL` | `gpt-4o-mini` | 支持视觉的模型效果更好；OpenCode Go 可用 `glm-5.3-flash` |
| `LLM_MAX_TOKENS` | `20000` | 推理模型会先消耗 reasoning token，太小会导致正式 content 为空 |
| `REVIVE_OUTLINED_TEXT` | `true` | 上传 / 强制重解析时尝试把转曲文字复活为 `<text>` |
| `OCR_ENABLED` | `true` | 启用本地 RapidOCR |
| `OCR_MIN_CONFIDENCE` | `0.62` | OCR 文字行最低置信度 |
| `REVIVAL_MIN_LINE_HEIGHT` | `8` | 低于该像素高度的 OCR 行忽略 |
| `REVIVAL_MIN_IOU` | `0.45` | 视觉文字行被现有 `<text>` 覆盖的判定阈值 |
| `LLM_VISION` | `true` | 是否把模板缩略图发给模型 |
| `FONT_DIR` | `static/Fonts` | 本地字体目录（ttf/otf） |
| `FONTS_ENABLED` | `true` | 是否把本地字体注入无头浏览器 |
| `PLAYWRIGHT_CHANNEL` | `msedge` | 复用本机 Edge；置空则用自带 Chromium |
| `POSTER_RENDER_SCALE` | `2` | 默认导出倍率（1–3） |
| `ENABLE_CORS` | `true` | 本地前端联调放开跨域 |

## 10. 测试

```powershell
python -m pip install -r requirements-dev.txt
pytest
```

测试不依赖 rembg / mediapipe（共 134 个用例）：

- **几何**：0.45h / 0.75h 构图、上下贴边、宽高受限、无脸退化、全身照放大、目标尺寸精确、
  alpha 保留、非法参数。
- **版式**：圆形蒙版四角透明、边缘抗锯齿、内侧描边不溢出；alpha 外轮廓 3% 边距、
  底部对齐、无 alpha 时退化。
- **海报模板**：图层 ID 补全、重复文字层移除、人像框/二维码框识别、大模型结果合并、
  文案规则解析、整段替换清空 tspan、空值保留默认文案、行首单字去重、`【...】` 高亮保留、
  本地字体别名发现、模板画廊/缩略图/解析、纯 SVG 预览、PNG 导出、素材上传与列表、
  未知模板 404（浏览器与 LLM 均以桩替代）。
- **图像处理**：编解码往返、预乘缩放不产生黑边、比例补边、rembg 缺失提示、rembg 返回值适配。
- **接口**：检测器用 stub 注入，覆盖 200 出图、404 文件、415 格式、422 坏图、413、
  手动人脸框、抠图失败降级、`REMBG_REQUIRED` 时 503；三种 mode 的默认值、圆框描边、
  全身强制抠图与非法参数。

## 11. 常见问题

| 现象 | 处理 |
|---|---|
| 首次 `process` 很慢 | 正在下载 u2net 模型（约 170 MB）到 `POSTER_CACHE_DIR/rembg`，后续走缓存 |
| 抠图报 503 / 提示未安装 rembg | `pip install rembg[cpu]`，或检查 onnxruntime 是否装好 |
| 下载模型报"拒绝访问" | 用户目录或 `%TEMP%` 不可写；设置 `POSTER_CACHE_DIR` 到可写的英文目录 |
| u2net 下载总是中断 | 用 curl 断点续传：`curl -L -C - --retry 100 --retry-all-errors -o "D:\DSH\.poster-cache\rembg\u2net.onnx" https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx`（md5 `60024c5c889badc19c04ad937298a77b`） |
| `mediapipe` 没有 `solutions` | 新版已移除旧 API，代码优先用 Tasks API，再降级 YuNet / Haar |
| OpenCV 5 检测不可用 | OpenCV 5 移除了 Haar 级联；运行 `python -m scripts.fetch_models` 用 YuNet |
| 中文路径加载模型失败 | 已内置 ASCII 缓存复制；必要时设置 `POSTER_CACHE_DIR=D:\poster-cache` |
| 全身模式报 503 | `full_body` 强制抠图，必须能加载 rembg 模型；先完成 u2net 模型下载 |
| 圆框没有背景 | 圆框模式默认不抠图；需要透明底请显式传 `remove_bg=true` |
| 成品边缘有灰边 | 确认走的是 `resize_rgba()`（预乘 alpha），不要直接用 `cv2.resize` |
| 输出很糊 | 原图人脸太小，放大倍数过高；`warnings` 会提示，换高清原图 |
| `data/outputs` 越来越大 | MVP 未做清理，可定时删 `data/outputs/*.png` |

## 12. 下一步

- 模板渲染：把裁切结果合成到海报模板（文字、色块、Logo）。
- 多人物照片：按人脸面积排序，取最大人脸或生成多套裁切。
- 手动框选：前端在检测失败时画框，回传 `face_x/y/w/h`（接口已支持）。
- 批量导出 ZIP、任务队列、结果缓存。

## 13. 本地拆层（方案 B · S2）

把「高清位图海报」在纯 CPU 上拆成「背景 + 前景图层 + 可编辑文字」，再组装为标准 SVG
导入编辑器。**独立环境 `.venv-decompose`**，不影响主 `.venv` 与现有编辑器。

```powershell
# 1) 一次性安装（纯 CPU：torch CPU + LayerD + PaddleOCR；首次约 2GB 下载）
powershell -ExecutionPolicy Bypass -File scripts\install_decompose.ps1

# 2) 单图冒烟（本机实测 1243×2244 海报约 9 分钟，峰值内存 4.5GB）
.\.venv-decompose\Scripts\python.exe scripts\decompose_smoke.py --image ".\samples\poster-demo.png"
```

产物在 `data\decompose\smoke-<时间>\`：`out.svg`、`composite.png`、`layers\`、`report.md`。
安装参数、环境变量（`HF_ENDPOINT` / `HF_HOME` / `LAMA_MODEL` / `PADDLE_PDX_CACHE_HOME` /
`DECOMPOSE_ASCII_CACHE`）、常见问题与后续 S3 接口衔接见
[docs/decompose-local.md](docs/decompose-local.md)。

> 注意：Paddle 的 C++ 推理层打不开含中文的路径，`PADDLE_PDX_CACHE_HOME` 必须是纯 ASCII 目录；
> 安装脚本在项目路径含中文时会自动改用 `<项目父目录>\poster-decompose-cache`（可用 `-AsciiCache` 覆盖），
> 并写入 `.env.decompose`。


# 本地拆层（方案 B · S2：环境安装与单图冒烟）

目标：把一张**高清位图海报**在**纯 CPU** 上拆成「背景 + 前景图层 + 可编辑文字」，
组装成标准 SVG 后导入现有编辑器。本阶段只交付**环境安装脚本**和**单图冒烟脚本**，
不修改主服务与前端（S3 才接 `/api/decompose`）。

```
AI 生成 PNG / 上传本地海报
        ↓
LayerD（CPU）逐层剥离 + LaMa 补底
        ↓
Pillow 紧致裁边 + 碎片过滤 + 坐标回映射
        ↓
PaddleOCR（CPU）整图识别 → 按行归属图层 → 局部擦除并转 <text>
        ↓
组装 SVG（base64 内嵌）→ template_import → 编辑器
```

## 1. 硬件与依赖约束

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 系统 | Windows 11 | 安装脚本按 **PowerShell 5.1** 语法编写（不使用 `??`） |
| CPU / 内存 | Intel 标压 + 16GB | 无 NVIDIA 显卡也能跑；单任务串行，实测峰值约 4.5GB |
| Python | 3.10 ~ 3.13（推荐 3.12） | 独立虚拟环境 `.venv-decompose`，**不碰主 `.venv`** |
| 磁盘 | ≥ 6GB 空闲 | 依赖约 1.5GB，模型约 1.2GB |

**严禁**：CUDA 版 PyTorch、`LayerD[ocr]`、`GOT-OCR2`（都需要 NVIDIA 显卡）。
本方案只安装 `LayerD` 核心推理包 + `device="cpu"` + 纯 CPU 版 PaddleOCR。

本机实测安装结果（2026-11）：`torch 2.15.0+cpu`（`torch.version.cuda is None`）、
`numpy 2.3.5`、`layerd 0.1.0`（commit `21aef93`）、`paddlepaddle 3.2.0`、
`paddleocr 3.7.0`（解析到 `paddlex 3.7.2`）、`opencv-contrib-python 4.10.0.84`。

## 2. 一次性安装

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_decompose.ps1
```

脚本会：

1. 创建 `.venv-decompose`（已存在则复用）；
2. 从 `download.pytorch.org/whl/cpu` 安装 `torch==2.15.0+cpu` / `torchvision==0.29.1+cpu`，
   并断言 `torch.version.cuda is None`；
3. 按 [requirements-decompose.txt](../requirements-decompose.txt) 安装 `numpy==2.3.5`
   （同时满足 `layerd>=2.0` 与 `paddlex<2.4`，脚本断言区间）、只保留一份
   `opencv-contrib-python==4.10.0.84`、`paddlepaddle==3.2.0` + `paddleocr==3.7.0`；
4. 安装 `hatchling` 后用 `--no-build-isolation --no-deps` 安装 LayerD（固定 commit）；
5. 预下载 BiRefNet（约 844MB，HF）、LaMa（196MB，GitHub Release）、PaddleOCR 检测/识别模型；
6. 生成 `.env.decompose` 与 `scripts\decompose_env.ps1`；
7. 自检并打印第一张图的冒烟命令。

### 常用参数

| 参数 | 作用 |
| --- | --- |
| `-DryRun` | 只打印命令，不下载、不写盘 |
| `-IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple` | PyPI 走国内镜像 |
| `-HfEndpoint https://huggingface.co` | HF 源（默认官方；`hf-mirror` 见常见问题） |
| `-SkipModels` | 暂不下载模型 |
| `-SkipPaddle` | 只装 LayerD，先验证拆层 |
| `-Recreate` | 删除并重建 `.venv-decompose`（只删这个目录） |
| `-PythonExe C:\Python312\python.exe` | 指定基础 Python |
| `-AsciiCache D:\DSH\poster-decompose-cache` | 指定 LaMa / Paddle 的纯 ASCII 缓存根 |

> **不要再手动执行 `pip install --upgrade pip`**：Windows 上 pip 替换自身时会空转。
> 安装脚本已跳过该步骤，并对所有 pip 调用使用 `--no-cache-dir`（损坏的 HTTP 缓存
> 会让 pip 在解析阶段空转几十分钟）。

## 3. 环境变量（模型缓存与镜像）

安装脚本写入项目根目录 `.env.decompose`（键值格式，不要提交到版本库）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DECOMPOSE_PYTHON` | `<项目>\.venv-decompose\Scripts\python.exe` | S3 子进程 worker 的解释器 |
| `DECOMPOSE_ASCII_CACHE` | `<项目父目录>\poster-decompose-cache`（路径含中文时） | LaMa / Paddle 的纯 ASCII 缓存根 |
| `HF_ENDPOINT` | `https://huggingface.co` | HF 源；`hf-mirror` 兼容性见常见问题 |
| `HF_HOME` | `data\models\huggingface` | BiRefNet 缓存（Python 读写，可含中文） |
| `LAMA_MODEL` | `<ASCII 缓存>\decompose\big-lama.pt` | 预下载的 LaMa 权重 |
| `PADDLE_PDX_CACHE_HOME` | `<ASCII 缓存>\paddle` | **必须纯 ASCII**：Paddle C++ 推理层打不开中文路径 |
| `PADDLE_PDX_MODEL_SOURCE` | `huggingface` | 可改 `modelscope` |
| `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` | `True` | 跳过首次的模型源连通性检查 |
| `DECOMPOSE_MAX_SIDE` | `1024` | 推理最大边长，可调 `1280` |

PowerShell 里临时生效：

```powershell
. .\scripts\decompose_env.ps1
```

## 4. 单图 CPU 冒烟

```powershell
# 1) 依赖/缓存自检（不加载大模型）
.\.venv-decompose\Scripts\python.exe scripts\decompose_smoke.py --self-check

# 2) 第一张图（LayerD + PaddleOCR）
.\.venv-decompose\Scripts\python.exe scripts\decompose_smoke.py `
  --image ".\samples\poster-demo.png"

# 只测拆层，不做 OCR（最快）
.\.venv-decompose\Scripts\python.exe scripts\decompose_smoke.py `
  --image ".\samples\poster-demo.png" --ocr none

# 保留所有文字为 PNG，仅检查拆层
.\.venv-decompose\Scripts\python.exe scripts\decompose_smoke.py `
  --image ".\samples\poster-demo.png" --keep-png-text

# 更快一档：少一次剥离迭代
.\.venv-decompose\Scripts\python.exe scripts\decompose_smoke.py `
  --image ".\samples\poster-demo.png" --max-iterations 2
```

产物目录：`data\decompose\smoke-<时间戳>\`

| 文件 | 内容 |
| --- | --- |
| `out.svg` | 组装后的可编辑 SVG（背景 + 残余图层 `<image>` + `<text>`，base64 内嵌） |
| `composite.png` | LayerD 重建合成图，用来核对拆层是否掉元素 |
| `layers\Lxx.png` | 每层擦除文字后的残余透明 PNG |
| `input_resized.png` | 实际送进推理的缩放图 |
| `report.md` / `report.json` | 阶段耗时、峰值内存、逐层 bbox / OCR 文本 / 置信度 / 覆盖率 / 判定原因 |

### 本机实测（1243×2244 教培海报，长边 1024，3 次迭代）

| 阶段 | 耗时 |
| --- | ---: |
| 加载 LayerD + LaMa | 20 ~ 32 s |
| 逐层剥离与补底 | 320 ~ 378 s |
| 加载 PaddleOCR | 10 ~ 12 s |
| 整图识别 + 图层判定 | 140 ~ 220 s |
| 组装 SVG | 约 1 s |
| **总计** | **约 9 ~ 10.5 分钟** |

峰值内存约 **4.5 ~ 4.8GB**，SVG 约 **1.6 ~ 1.8MB**，2 个前景层、12 ~ 13 个文字节点。
比原计划“1~3 分钟”慢，原因是本机 CPU 上 BiRefNet 每次迭代都要在 1024² 上推理，
且 PaddleOCR medium 模型在 CPU 上较慢。可用的提速手段见第 6 节。

> 已知限制：LayerD 在个别装饰条上会把同一段文字同时补进背景和前景，`composite.png`
> 里就能看到重影（不是 SVG 组装造成的）。这类区域建议在 S5 勾选“保留原图 PNG”，
> 或后续调高迭代次数/做局部修补；S3 会把 `composite.png` 一并返回给前端做自检。

## 5. 文字转换策略（为什么有的字不转）

- 对**整张海报只跑一次 OCR**，再把每个文字行按“与某层 alpha 的重叠率”归属到该层；
- 只有同时满足以下条件的行才转成 `<text>`：
  - OCR 置信度 ≥ 0.80；
  - 行内不透明像素覆盖率 ≥ 0.35；
  - **行高 ≤ 画布高度 4%**：主标题约 7%，保留 PNG；正文/副标题（约 2%~3%）转文字；
  - **行四周底色不是亮色装饰条**（亮度 > 170 的黄色课程条、白色徽章保留 PNG）。
- 文字颜色用“行内与图层底色差异最大的一簇”估计，红底白字不会被取成红色；
- 生成的 `<text>` 带 `textLength` + `lengthAdjust="spacingAndGlyphs"`，锁定原框宽，
  换字体后不会横向错位；
- 转换时会在**所有图层**上擦除该行区域（外扩 3px），并把背景层对应区域用四周底色抹平，
  再把残余图形留作 PNG：既避免“原文字没擦干净 + 新文字”重影，也避免 LaMa 补底时
  在背景里留下的文字残影；
- 被保留的行在 `report.md` 的「OCR 行判定」里写明行高与是否转换。

实践建议：先看 `composite.png` 确认拆层，再看 `out.svg` 确认文字层是否可接受；
装饰性标题如果被转坏了，用 `--keep-png-text` 或后续 S5 的逐层勾选保留 PNG。

## 6. 性能建议

| 想要 | 做法 | 代价 |
| --- | --- | --- |
| 拆层更快 | `--max-iterations 2` | 少剥离一层，复杂海报可能掉元素 |
| OCR 更快 | 后续 S3 增加 `small` 级 OCR 模型 / 降低检测边长 | 小字识别率下降 |
| 整图更快 | `--max-side 768`（当前脚本限定 1024/1280） | 细节与文字清晰度下降 |
| 内存更省 | 1280 不要开；保持 1024 | — |

首次运行包含模型读盘，第二次起加载会快一些。

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| `torch.version.cuda` 不是 `None` | 删除 `.venv-decompose` 重跑脚本（CPU 索引 + 断言） |
| pip 长时间占 CPU、没有下载 | 不要用旧缓存：脚本已加 `--no-cache-dir`；手动安装也请加上 |
| `hf-mirror.com` 报 “Distant resource does not seem to be on huggingface.co” | 新版 `huggingface_hub` 会校验重定向域名；用默认官方源，或升级/降级 hub 后重试 |
| Paddle 报 `Cannot open file ...inference.json` | 模型缓存路径含中文；把 `PADDLE_PDX_CACHE_HOME` 指到纯 ASCII 目录 |
| `import paddle` 报 `~/.cache/paddle` 权限错误 | 脚本已预建该目录；受限账户可让 `decompose_smoke.py` 自动把 `~` 指向 ASCII 缓存 |
| PaddleOCR 装不上 / 太重 | `-SkipPaddle` 先验证 LayerD；冒烟加 `--ocr none`，后续可切 `rapidocr` 兜底 |
| 中文路径导致 DLL 加载失败 | 用 `-VenvPath D:\venvs\poster-decompose` 换纯 ASCII 路径 |
| 想彻底重来 | `-Recreate`（只删 `.venv-decompose`）；模型缓存可保留 |

### 维护提示（改过脚本之后）

```powershell
# 安装脚本必须保持 UTF-8 with BOM（PS 5.1 会把无 BOM 的 UTF-8 当 ANSI）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev\parse_check_ps51.ps1 -Path scripts\install_decompose.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_decompose.ps1 -DryRun

# 冒烟脚本的纯逻辑（文字判定/擦除/SVG 组装/报告）可脱离模型验证
.\.venv\Scripts\python.exe scripts\dev\check_decompose_smoke.py
```

## 8. 与 S3 的衔接（下一阶段）

- `/api/decompose/health` 直接探测 `.venv-decompose\Scripts\python.exe`、包版本与
  `data\models` / ASCII 缓存，不用主进程 `find_spec`；
- 拆层任务通过子进程 worker 串行执行，进程退出即释放内存（本机实测峰值 4.5GB）；
- 冒烟脚本里的紧致裁边、坐标回映射、按行转文字、SVG 组装会抽到
  `app/services/local_decompose.py`，并补 pytest 单测（引擎打桩）；
- S6 在 `annotateCanvas` 标记可拖图层时排除 `data-decompose-layer="bg"`，避免误拖整张底图。

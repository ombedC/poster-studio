#requires -Version 5.1
# ===========================================================================
# ⚠ 编码要求：本文件必须保存为 UTF-8 with BOM（文件头 3 字节 EF BB BF）。
#   Windows PowerShell 5.1 会把「无 BOM 的 UTF-8」当成系统 ANSI 读取，中文会乱码，
#   甚至因为 GBK 双字节吞掉引号而直接语法报错。用 VS Code 保存时选
#   “UTF-8 with BOM”，或执行：
#     $p='scripts\install_decompose.ps1'
#     $t=[IO.File]::ReadAllText($p,[Text.Encoding]::UTF8)
#     [IO.File]::WriteAllText($p,$t,(New-Object Text.UTF8Encoding($true)))
# ===========================================================================
<#
.SYNOPSIS
  安装本地拆层环境（方案 B · S2）：纯 CPU PyTorch + LayerD + PaddleOCR → .venv-decompose

.DESCRIPTION
  1) 在项目根目录创建独立虚拟环境 .venv-decompose（绝不修改主 .venv）
  2) 安装纯 CPU 版 torch / torchvision（并断言 torch.version.cuda 为 None）
  3) 安装 LayerD 核心推理包（--no-deps，避免再装一份 opencv 与主环境冲突）
  4) 安装纯 CPU 版 paddlepaddle + paddleocr
  5) 预下载 BiRefNet（data\models）与 LaMa / PaddleOCR（纯 ASCII 缓存），生成 .env.decompose
  6) 运行自检并打印第一张图的冒烟命令

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_decompose.ps1 -DryRun

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_decompose.ps1
#>
[CmdletBinding()]
param(
    [string]$VenvPath = '',
    [string]$CacheRoot = '',
    [string]$PythonExe = '',
    [string]$AsciiCache = '',
    [string]$IndexUrl = 'https://pypi.org/simple',
    [string]$LayerdRef = '21aef937a0371614adb4d961f52d02409cb8ecc7',
    [string]$HfEndpoint = 'https://huggingface.co',
    [switch]$SkipModels,
    [switch]$SkipPaddle,
    [switch]$Recreate,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # 无控制台（如计划任务）时忽略
}
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    # 老系统不支持枚举时忽略
}

$Root = Split-Path -Parent $PSScriptRoot
if (-not $VenvPath) { $VenvPath = Join-Path $Root '.venv-decompose' }
if (-not $CacheRoot) { $CacheRoot = Join-Path $Root 'data\models' }
$Requirements = Join-Path $Root 'requirements-decompose.txt'
$script:Checker = Join-Path $Root 'scripts\check_decompose_env.py'
$LogPath = Join-Path $Root 'install-decompose.log'
$script:StepIndex = 0

function Test-AsciiPath {
    param([string]$Path)
    return $Path -eq ([System.Text.Encoding]::ASCII.GetString([System.Text.Encoding]::ASCII.GetBytes($Path)))
}

# Paddle 的 C++ 推理层打不开含中文的路径：模型缓存必须落在纯 ASCII 目录。
# HF 缓存由 Python 读写，可以留在项目目录（中文路径）。
function Get-AsciiCacheRoot {
    if ($AsciiCache) { return $AsciiCache }
    if (Test-AsciiPath $CacheRoot) { return $CacheRoot }
    # 往上找纯 ASCII 的祖先目录（项目父目录通常可写；盘符根目录常常不可写）
    $parent = Split-Path -Parent $Root
    while ($parent -and -not (Test-AsciiPath $parent)) {
        $next = Split-Path -Parent $parent
        if (-not $next -or $next -eq $parent) { break }
        $parent = $next
    }
    if ($parent -and (Test-AsciiPath $parent)) {
        return (Join-Path $parent 'poster-decompose-cache')
    }
    $local = $env:LOCALAPPDATA
    if ($local -and (Test-AsciiPath $local)) { return (Join-Path $local 'poster-decompose') }
    return 'C:\poster-decompose'
}

$script:AsciiCache = Get-AsciiCacheRoot
$script:PaddleCache = Join-Path $script:AsciiCache 'paddle'
$script:LamaPath = Join-Path $script:AsciiCache 'decompose\big-lama.pt'

# pip 的 HTTP 缓存曾导致解析阶段空转，这里统一禁用缓存（含 build 子进程）。
$env:PIP_NO_CACHE_DIR = '1'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_PROGRESS_BAR = 'off'

function Write-Step {
    param([string]$Message)
    $script:StepIndex += 1
    $line = '[{0:00}] {1}' -f $script:StepIndex, $Message
    Write-Host $line -ForegroundColor Cyan
    if (-not $DryRun) {
        Add-Content -LiteralPath $LogPath -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $Message) -Encoding UTF8
    }
}

function Write-Note {
    param([string]$Message)
    Write-Host ('     ' + $Message) -ForegroundColor DarkGray
}

function Write-Warn {
    param([string]$Message)
    Write-Host ('[!] ' + $Message) -ForegroundColor Yellow
    if (-not $DryRun) {
        Add-Content -LiteralPath $LogPath -Value ('[WARN] ' + $Message) -Encoding UTF8
    }
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$Label = ''
    )
    $display = '{0} {1}' -f $Exe, ($Arguments -join ' ')
    Write-Step ($(if ($Label) { $Label } else { '执行命令' }) + "`n     " + $display)
    if ($DryRun) { return }
    & $Exe @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw ('命令失败（exit {0}）：{1}' -f $code, $display)
    }
}

function Invoke-Pip {
    param([string[]]$PipArgs)
    Invoke-Native -Exe $script:Py -Arguments (@('-m', 'pip') + $PipArgs)
}

function Test-FreeSpace {
    try {
        $drive = New-Object -TypeName System.IO.DriveInfo -ArgumentList ([System.IO.Path]::GetPathRoot($Root))
        $freeGb = [math]::Round($drive.AvailableFreeSpace / 1GB, 1)
        Write-Note ('项目所在盘剩余空间：{0} GB' -f $freeGb)
        if ($freeGb -lt 6) {
            Write-Warn '剩余空间不足 6GB：模型 + 依赖约需 2.5~4GB，请先清理磁盘'
        }
        return $freeGb
    } catch {
        Write-Warn ('无法读取磁盘剩余空间：' + $_.Exception.Message)
        return $null
    }
}

function Resolve-BasePython {
    if ($PythonExe) {
        if (-not (Test-Path -LiteralPath $PythonExe)) {
            throw ('指定的 Python 不存在：' + $PythonExe)
        }
        return @{ Exe = $PythonExe; Prefix = @() }
    }
    $candidates = @(
        @{ Exe = 'py'; Prefix = @('-3.12') },
        @{ Exe = 'py'; Prefix = @('-3.11') },
        @{ Exe = 'py'; Prefix = @('-3') },
        @{ Exe = 'python'; Prefix = @() }
    )
    foreach ($item in $candidates) {
        if (-not (Get-Command $item.Exe -ErrorAction SilentlyContinue)) { continue }
        if ($DryRun) { return $item }
        & $item.Exe @($item.Prefix + @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)')) 2>$null
        if ($LASTEXITCODE -eq 0) { return $item }
    }
    throw '未找到 Python 3.10+。请先安装 Python（建议 3.12），或传 -PythonExe 指定解释器'
}

function New-DecomposeVenv {
    if ($Recreate -and (Test-Path -LiteralPath $VenvPath)) {
        $resolved = (Resolve-Path -LiteralPath $VenvPath).Path
        if ((Split-Path -Leaf $resolved) -ne '.venv-decompose') {
            throw ('拒绝删除非 .venv-decompose 目录：' + $resolved)
        }
        if (-not $resolved.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw ('拒绝删除项目外目录：' + $resolved)
        }
        Write-Warn ('按 -Recreate 删除旧环境：' + $resolved)
        if (-not $DryRun) { Remove-Item -LiteralPath $resolved -Recurse -Force }
    }
    if (Test-Path -LiteralPath $VenvPath) {
        $python = Join-Path $VenvPath 'Scripts\python.exe'
        if (Test-Path -LiteralPath $python) {
            Write-Note ('复用已存在的环境：' + $VenvPath)
            $script:Py = $python
            return
        }
        throw ('目录已存在但不是完整虚拟环境：' + $VenvPath + '（可加 -Recreate 重建）')
    }
    $base = Resolve-BasePython
    Invoke-Native -Exe $base.Exe -Arguments ($base.Prefix + @('-m', 'venv', $VenvPath)) -Label '创建独立虚拟环境'
    $script:Py = Join-Path $VenvPath 'Scripts\python.exe'
    if (-not $DryRun -and -not (Test-Path -LiteralPath $script:Py)) {
        throw ('虚拟环境创建失败，未找到：' + $script:Py)
    }
}

function Assert-CoreDeps {
    Write-Step '校验 torch(CPU) / numpy / opencv'
    if ($DryRun) { return }
    # 不拼 `python -c "..."`：PowerShell 5.1 会吞掉内层双引号，改用独立脚本。
    Invoke-Native -Exe $script:Py -Arguments @($script:Checker, '--stage', 'core') -Label '核心依赖检查'
    if ($LASTEXITCODE -ne 0) {
        throw '核心依赖校验失败：请检查 torch 是否为纯 CPU 版、numpy 是否在 [2.0, 2.4)'
    }
}

function Assert-AllDeps {
    Write-Step '校验 LayerD / PaddleOCR'
    if ($DryRun) { return }
    $checkArgs = @($script:Checker, '--stage', 'all')
    if ($SkipPaddle) { $checkArgs += '--skip-paddle' }
    Invoke-Native -Exe $script:Py -Arguments $checkArgs -Label '拆层依赖检查'
    if ($LASTEXITCODE -ne 0) {
        if ($SkipPaddle) {
            throw 'LayerD 校验失败：请检查网络/commit 是否可访问'
        }
        throw '拆层依赖校验失败。可加 -SkipPaddle 先只验证 LayerD，OCR 用 --ocr none / rapidocr 兜底'
    }
    $opencvPackages = @(& $script:Py -m pip list --format=freeze 2>$null | Select-String -Pattern '^opencv')
    if ($opencvPackages) {
        Write-Note ('已安装的 OpenCV 发行版：' + (($opencvPackages | ForEach-Object { $_.Line }) -join '; '))
    }
    if ($opencvPackages -match 'opencv-python-headless') {
        Write-Warn '同时存在 opencv-python-headless 与 opencv-contrib-python，可能互相覆盖 cv2/；建议只保留 contrib 4.10.0.84'
    }
}

function Set-DecomposeEnvironment {
    # 只在当前进程里生效，供后续模型下载 / 自检使用
    $env:HF_ENDPOINT = $HfEndpoint
    $env:HF_HOME = Join-Path $CacheRoot 'huggingface'
    $env:PADDLE_PDX_CACHE_HOME = $script:PaddleCache
    $env:PADDLE_PDX_MODEL_SOURCE = 'huggingface'
    $env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = 'True'
    $env:DECOMPOSE_ASCII_CACHE = $script:AsciiCache
    if (Test-Path -LiteralPath $script:LamaPath) { $env:LAMA_MODEL = $script:LamaPath }
    else { Remove-Item Env:\LAMA_MODEL -ErrorAction SilentlyContinue }

    if ($DryRun) { return }
    foreach ($dir in @($env:HF_HOME, $script:PaddleCache, (Split-Path -Parent $script:LamaPath))) {
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    }
    # paddle.dataset 在 import paddle 时会无条件创建 ~/.cache/paddle/dataset；
    # 受限账户下 Python 无法创建，这里先用当前 shell 建好。
    try {
        $paddleHome = Join-Path $env:USERPROFILE '.cache\paddle\dataset'
        if (-not (Test-Path -LiteralPath $paddleHome)) {
            New-Item -ItemType Directory -Path $paddleHome -Force | Out-Null
        }
    } catch {
        Write-Warn ('无法预建 ~/.cache/paddle/dataset：' + $_.Exception.Message)
    }
}

function Write-EnvFiles {
    Write-Step '生成本地环境配置（.env.decompose / scripts\decompose_env.ps1）'
    $lines = @(
        '# 自动生成：本地拆层环境（主服务后续读这个文件；不要提交到版本库）',
        ('DECOMPOSE_PYTHON=' + $script:Py),
        ('DECOMPOSE_VENV=' + $VenvPath),
        ('DECOMPOSE_ASCII_CACHE=' + $script:AsciiCache),
        ('HF_ENDPOINT=' + $HfEndpoint),
        ('HF_HOME=' + (Join-Path $CacheRoot 'huggingface')),
        ('LAMA_MODEL=' + $script:LamaPath),
        ('PADDLE_PDX_CACHE_HOME=' + $script:PaddleCache),
        'PADDLE_PDX_MODEL_SOURCE=huggingface',
        'PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True',
        'DECOMPOSE_MAX_SIDE=1024'
    )
    $ps1 = @(
        '# 自动生成：在 PowerShell 里 dot-source 本文件后再运行拆层脚本',
        ('$env:HF_ENDPOINT = ''' + $HfEndpoint + ''''),
        ('$env:HF_HOME = ''' + (Join-Path $CacheRoot 'huggingface') + ''''),
        ('$env:LAMA_MODEL = ''' + $script:LamaPath + ''''),
        ('$env:PADDLE_PDX_CACHE_HOME = ''' + $script:PaddleCache + ''''),
        '$env:PADDLE_PDX_MODEL_SOURCE = ''huggingface''',
        '$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = ''True'''
    )
    if ($DryRun) {
        Write-Note ('将写入 .env.decompose：' + ($lines -join ' | '))
        return
    }
    $utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllLines((Join-Path $Root '.env.decompose'), $lines, $utf8NoBom)
    $psPath = Join-Path $Root 'scripts\decompose_env.ps1'
    $utf8Bom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $true
    [System.IO.File]::WriteAllLines($psPath, $ps1, $utf8Bom)
    Write-Note ('.env.decompose 已写入项目根目录')
    Write-Note ('PowerShell 环境脚本：' + $psPath)
}

function Invoke-ModelDownload {
    if ($SkipModels) {
        Write-Warn '按 -SkipModels 跳过模型预下载，首次拆层时仍会自动下载'
        return
    }
    Write-Step '预下载模型（BiRefNet ~1.0GB / LaMa ~196MB / PaddleOCR 若干小模型）'
    $smoke = Join-Path $Root 'scripts\decompose_smoke.py'
    $arguments = @($smoke, '--download-models', '--cache-root', $CacheRoot)
    if ($HfEndpoint -and $HfEndpoint -ne 'https://huggingface.co') {
        $arguments += @('--hf-endpoint', $HfEndpoint)
    } else {
        # huggingface_hub >= 0.36 会校验重定向域名，hf-mirror 可能报
        # "Distant resource does not seem to be on huggingface.co"，默认走官方源。
        $arguments += '--no-hf-mirror'
    }
    if ($SkipPaddle) { $arguments += '--skip-ocr' }
    Invoke-Native -Exe $script:Py -Arguments $arguments -Label '下载模型'
}

function Invoke-SelfCheck {
    Write-Step '运行依赖与缓存自检'
    if ($DryRun) { return }
    $smoke = Join-Path $Root 'scripts\decompose_smoke.py'
    & $script:Py @($smoke, '--self-check', '--cache-root', $CacheRoot)
    if ($LASTEXITCODE -ne 0) {
        Write-Warn '自检未全部通过（详见上方输出）'
    }
}

function Get-EffectiveRequirements {
    # -SkipPaddle 时从清单里临时剔除 paddle 两行，但保留其余依赖
    if (-not $SkipPaddle) { return $Requirements }
    if ($DryRun) {
        Write-Note 'DryRun：正式安装时会临时剔除 requirements-decompose.txt 里的 paddlepaddle / paddleocr'
        return $Requirements
    }
    $lines = Get-Content -LiteralPath $Requirements | Where-Object { $_ -notmatch '^\s*paddle(paddle|ocr)?\b' }
    $tempDir = Join-Path $Root 'data\installers'
    if (-not (Test-Path -LiteralPath $tempDir)) { New-Item -ItemType Directory -Path $tempDir -Force | Out-Null }
    $temp = Join-Path $tempDir 'requirements-decompose.nopaddle.txt'
    $utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllLines($temp, $lines, $utf8NoBom)
    Write-Note ('按 -SkipPaddle 使用临时清单：' + $temp)
    return $temp
}

function Show-NextSteps {
    $python = Join-Path $VenvPath 'Scripts\python.exe'
    Write-Host ''
    Write-Host '================ 安装完成 ================' -ForegroundColor Green
    Write-Host ('拆层解释器：' + $python)
    Write-Host ('模型缓存：  ' + $CacheRoot)
    Write-Host ''
    Write-Host '第一次单图冒烟（本机实测约 9~10 分钟；路径换成你自己的海报）：' -ForegroundColor Cyan
    Write-Host ('  ' + $python + ' scripts\decompose_smoke.py --image ".\samples\poster-demo.png"')
    Write-Host ''
    Write-Host '如果只想先看拆层、不做 OCR（更快）：'
    Write-Host ('  ' + $python + ' scripts\decompose_smoke.py --image ".\samples\poster-demo.png" --ocr none --max-iterations 1')
    Write-Host ''
    Write-Host '产物在 data\decompose\smoke-<时间>\：out.svg / composite.png / layers\ / report.md'
    Write-Host '确认耗时与效果后，再进入 S3（后端 /api/decompose 服务）。'
    Write-Host '=========================================' -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
try {
    Write-Step '本地拆层环境安装开始（纯 CPU，不修改主 .venv）'
    Write-Note ('项目目录：' + $Root)
    Write-Note ('目标环境：' + $VenvPath)
    Write-Note ('模型缓存：' + $CacheRoot)
    if ($DryRun) { Write-Warn 'DryRun：只打印命令，不实际执行/写入' }

    if (-not (Test-Path -LiteralPath $Requirements)) {
        throw ('缺少依赖清单：' + $Requirements)
    }
    Test-FreeSpace | Out-Null
    Set-DecomposeEnvironment
    New-DecomposeVenv

    # 不要执行 `pip install --upgrade pip`：在 Windows 上 pip 替换自身时会空转。
    # venv 自带的 pip 24.2 足够安装本清单中的轮子。
    Invoke-Native -Exe $script:Py -Arguments @('-m', 'pip', '--version') -Label '检查 pip 版本'
    Invoke-Pip @('install', '--no-cache-dir', '--index-url', $IndexUrl, '-r', (Get-EffectiveRequirements))
    Assert-CoreDeps
    Invoke-Pip @('install', '--no-cache-dir', '--index-url', $IndexUrl, 'hatchling')

    Write-Step '安装 LayerD（--no-deps，避免重复安装 opencv-python-headless）'
    $layerdSpec = 'layerd @ git+https://github.com/CyberAgentAILab/LayerD.git@' + $LayerdRef
    $installed = $false
    if (Get-Command git -ErrorAction SilentlyContinue) {
        try {
            Invoke-Pip @('install', '--no-cache-dir', '--no-build-isolation', '--no-deps', '--index-url', $IndexUrl, $layerdSpec)
            $installed = $true
        } catch {
            Write-Warn ('git 安装失败：' + $_.Exception.Message)
        }
    } else {
        Write-Warn '未检测到 git，改用 zip 包安装 LayerD'
    }
    if (-not $installed) {
        $zipDir = Join-Path $Root 'data\installers'
        if (-not $DryRun -and -not (Test-Path -LiteralPath $zipDir)) {
            New-Item -ItemType Directory -Path $zipDir -Force | Out-Null
        }
        $zip = Join-Path $zipDir ('layerd-' + $LayerdRef + '.zip')
        if ($DryRun) {
            Write-Note ('将下载 https://codeload.github.com/CyberAgentAILab/LayerD/zip/' + $LayerdRef)
        } else {
            Invoke-WebRequest -Uri ('https://codeload.github.com/CyberAgentAILab/LayerD/zip/' + $LayerdRef) -OutFile $zip -UseBasicParsing
        }
        Invoke-Pip @('install', '--no-cache-dir', '--no-build-isolation', '--no-deps', '--index-url', $IndexUrl, $zip)
    }

    Set-DecomposeEnvironment
    Assert-AllDeps
    Write-EnvFiles
    Invoke-ModelDownload
    Invoke-SelfCheck
    Show-NextSteps
    if (-not $DryRun) {
        Add-Content -LiteralPath $LogPath -Value ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 安装完成') -Encoding UTF8
    }
    exit 0
} catch {
    Write-Host ''
    Write-Host ('安装失败：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ('完整日志：' + $LogPath) -ForegroundColor DarkGray
    Write-Host '常见处理：' -ForegroundColor Yellow
    Write-Host '  1) 网络问题：加 -IndexUrl https://pypi.tuna.tsinghua.edu.cn/simple'
    Write-Host '  2) 模型下载失败：加 -HfEndpoint https://huggingface.co 或稍后重试 -SkipModels'
    Write-Host '  3) Paddle 太重/装不上：加 -SkipPaddle 先只验证 LayerD 拆层'
    Write-Host '  4) 想重来：加 -Recreate（只会删除 .venv-decompose）'
    exit 1
}

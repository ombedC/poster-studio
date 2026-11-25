# 临时探针：验证「write 工具写入 U+FEFF → PowerShell 5.1 正确识别 UTF-8 中文」。
# 安装脚本已带 BOM；本文件不参与任何流程，可手动删除。
Write-Host '中文输出正常' -ForegroundColor Green

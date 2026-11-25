#requires -Version 5.1
<#
.SYNOPSIS
  Parse-check a PowerShell script with the Windows PowerShell 5.1 parser (no execution).

.DESCRIPTION
  PowerShell 5.1 reads UTF-8 without BOM as ANSI, so Chinese comments/strings can swallow
  quotes and break syntax. Run this after editing install_decompose.ps1:

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev\parse_check_ps51.ps1 -Path scripts\install_decompose.ps1

  Exit codes: 0 = OK, 1 = parse error, 2 = non-ASCII file without UTF-8 BOM.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path
)

$resolved = (Resolve-Path -LiteralPath $Path).Path
$tokens = $null
$errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($resolved, [ref]$tokens, [ref]$errors)

if ($errors.Count) {
    $errors | ForEach-Object {
        'PARSE ERROR @{0}: {1}' -f $_.Extent.StartLineNumber, $_.Message
    }
    exit 1
}

$bytes = [System.IO.File]::ReadAllBytes($resolved)
$hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191
$hasNonAscii = $false
foreach ($byte in $bytes) {
    if ($byte -gt 127) { $hasNonAscii = $true; break }
}

'PS 5.1 parse OK: {0} ({1} tokens)' -f $resolved, $tokens.Count
if ($hasNonAscii -and -not $hasBom) {
    Write-Host 'WARN: file contains non-ASCII bytes but has no UTF-8 BOM; PowerShell 5.1 may misparse it' -ForegroundColor Yellow
    exit 2
}
exit 0

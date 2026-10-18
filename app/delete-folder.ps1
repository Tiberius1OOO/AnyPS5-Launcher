#Requires -Version 5.1
<#
.SYNOPSIS
  Elevated helper: delete one game folder (used when normal delete hits Access Denied).
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$Target
)

$ErrorActionPreference = "Stop"
$path = $Target.Trim().Trim('"')
if (-not $path) {
    Write-Error "Missing -Target"
    exit 2
}
if (-not (Test-Path -LiteralPath $path)) {
    Write-Host "Already gone: $path"
    exit 0
}
Write-Host "Deleting (admin): $path"
Remove-Item -LiteralPath $path -Recurse -Force
if (Test-Path -LiteralPath $path) {
    Write-Error "Delete failed: folder still exists"
    exit 1
}
Write-Host "Deleted OK"
exit 0

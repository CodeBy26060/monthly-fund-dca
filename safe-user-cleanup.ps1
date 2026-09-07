[CmdletBinding()]
param(
    [int]$DownloadsOlderThanDays = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'SilentlyContinue'

$profileRoot = [Environment]::GetFolderPath('UserProfile')
$localAppDataRoot = Join-Path $profileRoot 'AppData\Local'
$tempRoot = Join-Path $localAppDataRoot 'Temp'
$downloadsRoot = Join-Path $profileRoot 'Downloads'

function Get-BytesInPath {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return [int64]0
    }

    $sum = [int64]0
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        Get-ChildItem -LiteralPath $Path -Force -File -Recurse |
            Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
            ForEach-Object { $sum += [int64]$_.Length }
    } else {
        $sum = [int64]$item.Length
    }

    return $sum
}

function Format-Bytes {
    param([int64]$Bytes)

    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} bytes' -f $Bytes)
}

function Add-ExistingPath {
    param(
        [Parameter(Mandatory)]
        [System.Collections.Generic.List[string]]$List,
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (Test-Path -LiteralPath $Path) {
        [void]$List.Add($Path)
    }
}

function Get-CacheDirectories {
    $paths = [System.Collections.Generic.List[string]]::new()

    $edgeRoot = Join-Path $localAppDataRoot 'Microsoft\Edge\User Data'
    $chromeRoot = Join-Path $localAppDataRoot 'Google\Chrome\User Data'
    $firefoxRoot = Join-Path $localAppDataRoot 'Mozilla\Firefox\Profiles'

    foreach ($browserRoot in @($edgeRoot, $chromeRoot)) {
        if (Test-Path -LiteralPath $browserRoot) {
            Get-ChildItem -LiteralPath $browserRoot -Directory -Force |
                Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' } |
                ForEach-Object {
                    foreach ($relativePath in @(
                        'Cache',
                        'Code Cache',
                        'GPUCache',
                        'Service Worker\CacheStorage'
                    )) {
                        Add-ExistingPath -List $paths -Path (Join-Path $_.FullName $relativePath)
                    }
                }
        }
    }

    if (Test-Path -LiteralPath $firefoxRoot) {
        Get-ChildItem -LiteralPath $firefoxRoot -Directory -Force |
            ForEach-Object {
                foreach ($relativePath in @('cache2', 'startupCache')) {
                    Add-ExistingPath -List $paths -Path (Join-Path $_.FullName $relativePath)
                }
            }
    }

    return @($paths | Select-Object -Unique)
}

function Get-ChildrenToRemove {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return @()
    }

    return @(Get-ChildItem -LiteralPath $Path -Force |
        Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) })
}

$cacheDirectories = @(Get-CacheDirectories)
$cleanupTargets = [System.Collections.Generic.List[object]]::new()

if (Test-Path -LiteralPath $tempRoot) {
    [void]$cleanupTargets.Add([pscustomobject]@{
        Kind = 'User temporary files'
        Path = $tempRoot
        Items = @(Get-ChildrenToRemove -Path $tempRoot)
    })
}

foreach ($cachePath in $cacheDirectories) {
    [void]$cleanupTargets.Add([pscustomobject]@{
        Kind = 'Browser cache'
        Path = $cachePath
        Items = @(Get-ChildrenToRemove -Path $cachePath)
    })
}

$downloadCutoff = (Get-Date).AddDays(-$DownloadsOlderThanDays)
$oldDownloadItems = @()
if (Test-Path -LiteralPath $downloadsRoot) {
    $oldDownloadItems = @(Get-ChildItem -LiteralPath $downloadsRoot -File -Force -Recurse |
        Where-Object {
            $_.LastWriteTime -lt $downloadCutoff -and
            -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)
        })
}

$recycleBinBytes = [int64]0
try {
    $recycleBinItems = @(Get-ChildItem -LiteralPath 'C:\$Recycle.Bin' -File -Force -Recurse)
    $recycleBinBytes = [int64](($recycleBinItems | Measure-Object -Property Length -Sum).Sum)
} catch {
    $recycleBinItems = @()
}

$plannedBytes = [int64]0
Write-Host ''
Write-Host 'SAFE CLEANUP PREVIEW' -ForegroundColor Cyan
Write-Host "User profile: $profileRoot"
Write-Host "Downloads cutoff: $($downloadCutoff.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host ''

foreach ($target in $cleanupTargets) {
    $bytes = [int64](($target.Items | Measure-Object -Property Length -Sum).Sum)
    $plannedBytes += $bytes
    Write-Host ("[{0}] {1} | {2} | {3} item(s)" -f $target.Kind, $target.Path, (Format-Bytes $bytes), $target.Items.Count)
}

$downloadBytes = [int64](($oldDownloadItems | Measure-Object -Property Length -Sum).Sum)
$plannedBytes += $downloadBytes
Write-Host ("[Old downloads] {0} | {1} | {2} file(s)" -f $downloadsRoot, (Format-Bytes $downloadBytes), $oldDownloadItems.Count)
Write-Host ("[Recycle Bin] C:\`$Recycle.Bin | {0} | {1} file(s)" -f (Format-Bytes $recycleBinBytes), $recycleBinItems.Count)
$plannedBytes += $recycleBinBytes

Write-Host ''
Write-Host ("Planned maximum reclaim: {0}" -f (Format-Bytes $plannedBytes)) -ForegroundColor Yellow
Write-Host 'Protected by this script: System32, WinSxS, pagefile.sys, hiberfil.sys, and user files outside the listed targets.'
Write-Host ''

$confirmation = Read-Host 'Type Y or YES to continue. Any other input cancels'
if ($confirmation -notmatch '^(?i:y|yes)$') {
    Write-Host 'Cancelled. No files were changed.' -ForegroundColor Yellow
    exit 0
}

$deletedBytes = [int64]0
$skippedCount = 0

foreach ($target in $cleanupTargets) {
    foreach ($item in $target.Items) {
        try {
            $length = if ($item.PSIsContainer) { Get-BytesInPath -Path $item.FullName } else { [int64]$item.Length }
            Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
            $deletedBytes += $length
        } catch {
            $skippedCount++
        }
    }
}

foreach ($item in $oldDownloadItems) {
    try {
        Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop
        $deletedBytes += [int64]$item.Length
    } catch {
        $skippedCount++
    }
}

try {
    Clear-RecycleBin -DriveLetter C -Force -ErrorAction Stop
    $deletedBytes += $recycleBinBytes
} catch {
    Write-Warning 'Recycle Bin could not be emptied. It may require an elevated PowerShell window.'
}

Write-Host ''
Write-Host 'Cleanup complete.' -ForegroundColor Green
Write-Host ("Estimated deleted size: {0}" -f (Format-Bytes $deletedBytes))
Write-Host ("Skipped/locked items: {0}" -f $skippedCount)
Write-Host 'Some files may remain if they were in use by a running application.'

[CmdletBinding()]
param(
    [int]$DownloadsOlderThanDays = 30,
    [switch]$Delete
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'SilentlyContinue'

$global:TotalFilesCount = 0
$global:TotalSizeBytes = [int64]0
$script:ScanStartTime = Get-Date

$profileRoot = [Environment]::GetFolderPath('UserProfile')
$localAppData = Join-Path $profileRoot 'AppData\Local'
$appDataRoot = Join-Path $profileRoot 'AppData'
$downloadsRoot = Join-Path $profileRoot 'Downloads'
$userTempRoot = Join-Path $localAppData 'Temp'
$windowsTempRoot = 'C:\Windows\Temp'

# Hard-coded safety exclusions. The exclusion check runs before any deletion.
$excludedRoots = @(
    'C:\Windows',
    'C:\Program Files',
    'C:\Program Files (x86)',
    'C:\Users\Default',
    (Join-Path $profileRoot 'Desktop'),
    (Join-Path $profileRoot 'Documents')
) | ForEach-Object { $_.TrimEnd('\') }

function Normalize-Path {
    param([Parameter(Mandatory)][string]$Path)
    try {
        return [IO.Path]::GetFullPath($Path).TrimEnd('\')
    } catch {
        return $Path.TrimEnd('\')
    }
}

function Test-IsExcludedPath {
    param([Parameter(Mandatory)][string]$Path)

    $candidate = (Normalize-Path $Path).ToLowerInvariant()
    foreach ($excluded in $excludedRoots) {
        $root = (Normalize-Path $excluded).ToLowerInvariant()
        if ($candidate -eq $root -or $candidate.StartsWith($root + '\')) {
            return $true
        }
    }
    return $false
}

function Test-IsProtectedAppDataFile {
    param([Parameter(Mandatory)][string]$Path)

    $candidate = (Normalize-Path $Path).ToLowerInvariant()
    $appDataPrefix = (Normalize-Path $appDataRoot).ToLowerInvariant() + '\'
    if (-not $candidate.StartsWith($appDataPrefix)) {
        return $false
    }

    # Never delete AppData files ending in .config or .db.
    return ($candidate -match '(?i)(\.config|\.db)$')
}

function Get-FilesUnderRoot {
    param([Parameter(Mandatory)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return @()
    }

    return @(Get-ChildItem -LiteralPath $Root -File -Force -Recurse |
        Where-Object {
            -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
            -not (Test-IsExcludedPath -Path $_.FullName)
        })
}

function Get-Bytes {
    param([object[]]$Files)

    if ($null -eq $Files -or $Files.Count -eq 0) {
        return [int64]0
    }
    return [int64](($Files | Measure-Object -Property Length -Sum).Sum)
}

function Format-Bytes {
    param([int64]$Bytes)

    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N0} MB' -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ('{0:N0} KB' -f ($Bytes / 1KB)) }
    return ('{0:N0} bytes' -f $Bytes)
}

function Write-CleanupSummary {
    $elapsedSeconds = ((Get-Date) - $script:ScanStartTime).TotalSeconds

    Write-Host ''
    Write-Host '========== 清理总结报告 ==========' -ForegroundColor Cyan
    Write-Host ("本次扫描共发现待清理文件：{0} 个" -f $global:TotalFilesCount) -ForegroundColor Green
    Write-Host ("预计释放空间：{0:N2} GB" -f ($global:TotalSizeBytes / 1GB)) -ForegroundColor Yellow
    Write-Host ("扫描耗时：{0:N2} 秒" -f $elapsedSeconds) -ForegroundColor Green
    Write-Host '====================================' -ForegroundColor Cyan
}

function Add-ExistingCacheRoot {
    param(
        [Parameter(Mandatory)]
        [System.Collections.Generic.List[string]]$List,
        [Parameter(Mandatory)]
        [string]$Path
    )

    if ((Test-Path -LiteralPath $Path -PathType Container) -and
        -not (Test-IsExcludedPath -Path $Path)) {
        [void]$List.Add((Normalize-Path $Path))
    }
}

function Get-CacheRoots {
    $roots = [System.Collections.Generic.List[string]]::new()

    foreach ($browserRoot in @(
        (Join-Path $localAppData 'Google\Chrome\User Data'),
        (Join-Path $localAppData 'Microsoft\Edge\User Data')
    )) {
        if (Test-Path -LiteralPath $browserRoot -PathType Container) {
            Get-ChildItem -LiteralPath $browserRoot -Directory -Force |
                Where-Object { $_.Name -eq 'Default' -or $_.Name -like 'Profile *' } |
                ForEach-Object {
                    foreach ($relativePath in @(
                        'Cache',
                        'Code Cache',
                        'GPUCache',
                        'Service Worker\CacheStorage'
                    )) {
                        Add-ExistingCacheRoot -List $roots -Path (Join-Path $_.FullName $relativePath)
                    }
                }
        }
    }

    $firefoxRoot = Join-Path $localAppData 'Mozilla\Firefox\Profiles'
    if (Test-Path -LiteralPath $firefoxRoot -PathType Container) {
        Get-ChildItem -LiteralPath $firefoxRoot -Directory -Force |
            ForEach-Object {
                foreach ($relativePath in @('cache2', 'startupCache')) {
                    Add-ExistingCacheRoot -List $roots -Path (Join-Path $_.FullName $relativePath)
                }
            }
    }

    foreach ($cacheRoot in @(
        (Join-Path $localAppData 'npm-cache'),
        (Join-Path $appDataRoot 'npm-cache'),
        (Join-Path $localAppData 'pip\Cache'),
        (Join-Path $appDataRoot 'pip\Cache')
    )) {
        Add-ExistingCacheRoot -List $roots -Path $cacheRoot
    }

    return @($roots | Select-Object -Unique)
}

$downloadCutoff = (Get-Date).AddDays(-$DownloadsOlderThanDays)
$cacheRoots = @(Get-CacheRoots)
$scannedFileCount = 0
$candidateFiles = [System.Collections.Generic.List[object]]::new()
$protectedCount = 0
$filteredOutCount = 0

function Add-Candidates {
    param(
        [Parameter(Mandatory)][object[]]$Files,
        [Parameter(Mandatory)][string]$Reason,
        [scriptblock]$Filter = { $true }
    )

    foreach ($file in $Files) {
        $script:scannedFileCount++
        if (-not (& $Filter $file)) {
            $script:filteredOutCount++
        } elseif (Test-IsProtectedAppDataFile -Path $file.FullName) {
            $script:protectedCount++
        } else {
            $global:TotalFilesCount++
            $global:TotalSizeBytes += [int64]$file.Length
            [void]$script:candidateFiles.Add([pscustomobject]@{
                    Reason = $Reason
                    SizeBytes = [int64]$file.Length
                    Path = $file.FullName
                    LastWriteTime = $file.LastWriteTime
                })
        }
    }
}

Write-Host ''
Write-Host 'CLEANUP DRY RUN' -ForegroundColor Cyan
Write-Host "User profile: $profileRoot"
Write-Host "Downloads cutoff: $($downloadCutoff.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host ''
Write-Host 'Excluded roots (never scanned for deletion and never deleted):' -ForegroundColor Yellow
$excludedRoots | ForEach-Object { Write-Host "  $_" }
Write-Host "  $windowsTempRoot (also excluded by C:\Windows)"
Write-Host ''

Add-Candidates -Files @(Get-FilesUnderRoot -Root $userTempRoot) -Reason 'User Temp'

foreach ($cacheRoot in $cacheRoots) {
    Add-Candidates -Files @(Get-FilesUnderRoot -Root $cacheRoot) -Reason 'Software cache'
}

if (Test-Path -LiteralPath $downloadsRoot -PathType Container) {
    $downloadFiles = @(Get-FilesUnderRoot -Root $downloadsRoot)
    Add-Candidates -Files $downloadFiles -Reason 'Downloads older than cutoff' -Filter {
        param($file)
        $file.LastWriteTime -lt $downloadCutoff
    }
}

$candidateFiles = @($candidateFiles | Sort-Object Path -Unique)
$plannedBytes = Get-Bytes -Files $candidateFiles

Write-Host 'Files marked for deletion:' -ForegroundColor Green
if ($candidateFiles.Count -eq 0) {
    Write-Host '  None'
} else {
    foreach ($file in $candidateFiles) {
        Write-Host ("  [{0}] {1} | {2}" -f $file.Reason, (Format-Bytes $file.SizeBytes), $file.Path)
    }
}

Write-Host ''
Write-Host ("Scanned files: {0}" -f $scannedFileCount)
Write-Host ("Protected .config/.db files: {0}" -f $protectedCount)
Write-Host ("Filtered out by age/rule: {0}" -f $filteredOutCount)
Write-Host ("Estimated reclaimable space: {0} ({1:N2} GB)" -f (Format-Bytes $plannedBytes), ($plannedBytes / 1GB)) -ForegroundColor Yellow

if (-not $Delete) {
    Write-Host ''
    Write-Host 'Dry Run only: no files were deleted. Re-run with -Delete to enable deletion.' -ForegroundColor Cyan
    Write-CleanupSummary
    return
}

Write-Host ''
Write-Host 'Deletion mode was requested. Review the list above carefully.' -ForegroundColor Red
$confirmation = Read-Host 'Type DELETE exactly to continue; any other input cancels'
if ($confirmation -cne 'DELETE') {
    Write-Host 'Cancelled. No files were changed.' -ForegroundColor Yellow
    Write-CleanupSummary
    return
}

$deletedBytes = [int64]0
$failedCount = 0
foreach ($file in $candidateFiles) {
    try {
        $isExcluded = Test-IsExcludedPath -Path $file.Path
        $isProtected = Test-IsProtectedAppDataFile -Path $file.Path
        if ($isExcluded -or $isProtected) {
            $failedCount++
            continue
        }

        Remove-Item -LiteralPath $file.Path -Force -ErrorAction Stop
        $deletedBytes += [int64]$file.SizeBytes
    } catch {
        $failedCount++
    }
}

Write-Host ''
Write-Host 'Deletion complete.' -ForegroundColor Green
Write-Host ("Deleted files: {0}" -f ($candidateFiles.Count - $failedCount))
Write-Host ("Skipped/failed files: {0}" -f $failedCount)
Write-Host ("Estimated space released: {0} ({1:N2} GB)" -f (Format-Bytes $deletedBytes), ($deletedBytes / 1GB))
Write-CleanupSummary

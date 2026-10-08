# check_prereqs.ps1 -- Windows prerequisite checker for the secretary method kit.
#
# WHY THIS IS NOT PYTHON: this script's whole job is to find out whether Python
# exists. It must run on a machine with nothing installed, so it uses only
# PowerShell, which ships with Windows.
#
# WHY THIS FILE IS ASCII-ONLY (no Japanese): PowerShell 5.1 reads a BOM-less
# UTF-8 file as cp932 and dies with a syntax error on Japanese text, and a
# cp932 console mangles Japanese output anyway. So the script emits machine
# readable ASCII and Claude explains the result to the user in Japanese.
#
# KEEP IN SYNC WITH check_prereqs.sh -- same keys, same verdicts, same
# required/optional split. A disagreement between the two means one platform
# gets a wrong answer.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File tools\check_prereqs.ps1
# Output: KEY=VALUE lines.
#   RESULT=ok            -> everything REQUIRED is present
#   RESULT=action_needed -> see MISSING (required) / OPTIONAL_MISSING (optional)
#
# REQUIRED: python, git      OPTIONAL: node (Google Chat only), gh (backup only)

$ErrorActionPreference = 'SilentlyContinue'

$MIN_PYTHON = [Version]'3.9'
$MIN_NODE = 18

function Write-Kv($key, $value) { Write-Output "$key=$value" }

function Get-CmdPath($name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    return $null
}

# Returns a hashtable: Ok (did the command actually succeed), Version, Raw.
# NOTE: do not name the second parameter $args -- that is a PowerShell
# automatic variable and shadowing it silently breaks the invocation.
# NOTE: a version-looking substring in the output is NOT proof of success, so
# the native exit code is checked too (a broken install can still print text).
function Get-ToolVersion($exe, $verArg) {
    $result = @{ Ok = $false; Version = $null; Raw = '' }
    try {
        $global:LASTEXITCODE = 0
        $raw = & $exe $verArg 2>&1 | Out-String
        $code = $LASTEXITCODE
    } catch {
        return $result
    }
    $result.Raw = $raw
    if ($code -ne 0) { return $result }
    $m = [regex]::Match($raw, '(\d+)\.(\d+)(\.(\d+))?')
    if (-not $m.Success) { return $result }
    $result.Version = $m.Value
    $result.Ok = $true
    return $result
}

# Pre-release builds report a version that compares as stable (3.9.0rc1 -> 3.9.0)
# but are not what we want people running. CPython uses 3.13.0a1 / 3.13.0b2 /
# 3.13.0rc1, so the digit-letter-digit form has to be matched too -- not just
# the spelled-out words.
function Test-Prerelease($raw) {
    return ($raw -match '(?i)(\d(a|b|rc)\d+|alpha|beta|[-+]dev|nightly|canary)')
}

Write-Kv 'PREREQ_CHECK_VERSION' '2'
Write-Kv 'OS' 'windows'
Write-Kv 'OS_VERSION' ([System.Environment]::OSVersion.Version.ToString())
Write-Kv 'PS_VERSION' ($PSVersionTable.PSVersion.ToString())

$missing = New-Object System.Collections.ArrayList          # required
$optionalMissing = New-Object System.Collections.ArrayList  # nice to have
$blocked = New-Object System.Collections.ArrayList

# ---- Python (REQUIRED) -----------------------------------------------------
# On Windows the correct launcher is `py`. A bare `python` may resolve to the
# Microsoft Store stub, which is NOT an interpreter: it just opens the Store.
$pythonCmd = $null
$pythonInfo = $null

$pyPath = Get-CmdPath 'py'
if ($pyPath) {
    $info = Get-ToolVersion 'py' '--version'
    if ($info.Ok) { $pythonCmd = 'py'; $pythonInfo = $info }
}
if (-not $pythonCmd) {
    $pyExe = Get-CmdPath 'python'
    if ($pyExe -and $pyExe -like '*\WindowsApps\*') {
        Write-Kv 'PYTHON' 'store_stub'
        Write-Kv 'PYTHON_STUB_PATH' $pyExe
        [void]$missing.Add('python')
    } elseif ($pyExe) {
        $info = Get-ToolVersion 'python' '--version'
        if ($info.Ok) {
            $pythonCmd = 'python'
            $pythonInfo = $info
        } else {
            Write-Kv 'PYTHON' 'broken'
            [void]$missing.Add('python')
        }
    } else {
        if (-not $pyPath) {
            Write-Kv 'PYTHON' 'missing'
            [void]$missing.Add('python')
        } else {
            Write-Kv 'PYTHON' 'broken'
            [void]$missing.Add('python')
        }
    }
}
if ($pythonCmd) {
    Write-Kv 'PYTHON_CMD' $pythonCmd
    Write-Kv 'PYTHON_VERSION' $pythonInfo.Version
    if (Test-Prerelease $pythonInfo.Raw) {
        Write-Kv 'PYTHON' 'prerelease'
        [void]$missing.Add('python')
    } else {
        $ok = $false
        try { $ok = ([Version]$pythonInfo.Version) -ge $MIN_PYTHON } catch { $ok = $false }
        if ($ok) {
            Write-Kv 'PYTHON' 'ok'
        } else {
            Write-Kv 'PYTHON' 'too_old'
            [void]$missing.Add('python')
        }
    }
}

# ---- git (REQUIRED) --------------------------------------------------------
# Presence on PATH is not proof it works, so require a parsable version --
# the same bar the macOS side uses for its Xcode shim case.
if (Get-CmdPath 'git') {
    $gitInfo = Get-ToolVersion 'git' '--version'
    if ($gitInfo.Ok) {
        Write-Kv 'GIT' 'ok'
        Write-Kv 'GIT_VERSION' $gitInfo.Version
        $n = & git config --global user.name 2>$null
        $e = & git config --global user.email 2>$null
        if ($n -and $e) { Write-Kv 'GIT_IDENTITY' 'ok' } else { Write-Kv 'GIT_IDENTITY' 'unset' }
    } else {
        Write-Kv 'GIT' 'broken'
        [void]$missing.Add('git')
    }
} else {
    Write-Kv 'GIT' 'missing'
    [void]$missing.Add('git')
}

# ---- Node.js (OPTIONAL -- Google Chat connection only) ---------------------
if (Get-CmdPath 'node') {
    $nodeInfo = Get-ToolVersion 'node' '--version'
    if ($nodeInfo.Ok) {
        Write-Kv 'NODE_VERSION' $nodeInfo.Version
        if (Test-Prerelease $nodeInfo.Raw) {
            Write-Kv 'NODE' 'prerelease'
            [void]$optionalMissing.Add('node')
        } else {
            $major = [int]($nodeInfo.Version.Split('.')[0])
            if ($major -ge $MIN_NODE) {
                Write-Kv 'NODE' 'ok'
            } else {
                Write-Kv 'NODE' 'too_old'
                [void]$optionalMissing.Add('node')
            }
        }
    } else {
        Write-Kv 'NODE' 'broken'
        [void]$optionalMissing.Add('node')
    }
} else {
    Write-Kv 'NODE' 'missing'
    [void]$optionalMissing.Add('node')
}

# ---- GitHub CLI (OPTIONAL -- backup to GitHub only) ------------------------
if (Get-CmdPath 'gh') {
    $ghInfo = Get-ToolVersion 'gh' '--version'
    if ($ghInfo.Ok) {
        Write-Kv 'GH' 'ok'
        Write-Kv 'GH_VERSION' $ghInfo.Version
        & gh auth status 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Kv 'GH_AUTH' 'ok' } else { Write-Kv 'GH_AUTH' 'unset' }
    } else {
        Write-Kv 'GH' 'broken'
        [void]$optionalMissing.Add('gh')
    }
} else {
    Write-Kv 'GH' 'missing'
    [void]$optionalMissing.Add('gh')
}

# ---- package manager -------------------------------------------------------
if (Get-CmdPath 'winget') {
    $wgInfo = Get-ToolVersion 'winget' '--version'
    Write-Kv 'PKGMGR' 'winget'
    if ($wgInfo.Ok) { Write-Kv 'PKGMGR_VERSION' $wgInfo.Version }
} else {
    Write-Kv 'PKGMGR' 'none'
    [void]$blocked.Add('winget_missing')
}

# ---- rights / policy -------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Kv 'ADMIN' 'true'
} else {
    Write-Kv 'ADMIN' 'false'
}
Write-Kv 'EXEC_POLICY' (Get-ExecutionPolicy -Scope CurrentUser)

# ---- summary ---------------------------------------------------------------
Write-Kv 'MISSING' ($missing -join ',')
Write-Kv 'OPTIONAL_MISSING' ($optionalMissing -join ',')
Write-Kv 'BLOCKED' ($blocked -join ',')
if ($missing.Count -eq 0) { Write-Kv 'RESULT' 'ok' } else { Write-Kv 'RESULT' 'action_needed' }

# secretary-method-tool: check_prereqs v0.10

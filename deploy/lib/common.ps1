# ============================================================
# MarxLen 部署脚本公共库
#
# 提供：日志输出、步骤状态记录（断点续跑）、错误中断、下载。
# 被 install.ps1 / start.ps1 / stop.ps1 共同引用。
#
# 设计约束：
#   1. 必须兼容 Windows PowerShell 5.1（裸机自带），
#      因此不能用 PS7 的 ?? 、?. 、-ErrorAction Ignore 等新语法。
#   2. 所有输出面向完全不懂技术的用户，禁止抛裸异常堆栈。
# ============================================================

# 严格模式：未定义变量、错误调用直接报错，避免静默出错
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 项目根目录（本文件在 deploy/lib/ 下，故上溯两级）
$script:ROOT = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# 部署产生的一切中间文件都放在 deploy/ 下，便于整体清理
$script:DEPLOY_DIR = Join-Path $script:ROOT "deploy"
$script:STATE_FILE = Join-Path $script:DEPLOY_DIR "state.json"
$script:LOG_FILE = Join-Path $script:DEPLOY_DIR "install.log"
$script:PY_DIR = Join-Path $script:ROOT "python-embed"
$script:CACHE_DIR = Join-Path $script:DEPLOY_DIR "cache"


# ── 日志 ────────────────────────────────────────────────────

function Write-Log {
    <#
      同时写屏幕与日志文件。
      日志文件是排查问题的唯一凭据，用户报错时让他把它发来即可。
    #>
    param(
        [string]$Message,
        [string]$Level = "INFO"
    )
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$stamp] [$Level] $Message"
    try {
        if (-not (Test-Path $script:DEPLOY_DIR)) {
            New-Item -ItemType Directory -Path $script:DEPLOY_DIR -Force | Out-Null
        }
        Add-Content -Path $script:LOG_FILE -Value $line -Encoding UTF8
    } catch {
        # 日志写不进去也不能让主流程崩掉
    }
}

function Say {
    <# 普通信息：白字 #>
    param([string]$Text)
    Write-Host $Text
    Write-Log $Text "INFO"
}

function Say-Step {
    <# 阶段标题：醒目分隔，让用户知道进行到哪一步 #>
    param([string]$Text)
    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Log "STEP: $Text" "INFO"
}

function Say-Ok {
    param([string]$Text)
    Write-Host "  [完成] $Text" -ForegroundColor Green
    Write-Log "OK: $Text" "INFO"
}

function Say-Skip {
    param([string]$Text)
    Write-Host "  [跳过] $Text" -ForegroundColor DarkGray
    Write-Log "SKIP: $Text" "INFO"
}

function Say-Warn {
    param([string]$Text)
    Write-Host "  [提醒] $Text" -ForegroundColor Yellow
    Write-Log "WARN: $Text" "WARN"
}

function Say-Info {
    <# 次要说明：缩进灰字，不抢主流程的注意力 #>
    param([string]$Text)
    Write-Host "         $Text" -ForegroundColor DarkGray
    Write-Log "  $Text" "INFO"
}


# ── 错误中断 ────────────────────────────────────────────────

function Stop-WithError {
    <#
      带解决方案的中断。

      普通用户看到"报错"就懵，所以每次中断必须回答三个问题：
      出了什么事、为什么、他现在该做什么。
      $Solutions 就是"该做什么"，必须具体到可执行。
    #>
    param(
        [string]$Title,
        [string]$Reason = "",
        [string[]]$Solutions = @()
    )
    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Red
    Write-Host "  安装中断：$Title" -ForegroundColor Red
    Write-Host "===============================================================" -ForegroundColor Red
    if ($Reason) {
        Write-Host ""
        Write-Host "原因：" -ForegroundColor Yellow
        Write-Host "  $Reason"
    }
    if ($Solutions.Count -gt 0) {
        Write-Host ""
        Write-Host "请这样处理：" -ForegroundColor Yellow
        $i = 1
        foreach ($s in $Solutions) {
            Write-Host "  $i. $s"
            $i++
        }
    }
    Write-Host ""
    Write-Host "已完成的步骤会被记住，解决问题后重新运行本脚本即可接着装，" -ForegroundColor DarkGray
    Write-Host "不会从头再来。" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "详细日志：$script:LOG_FILE" -ForegroundColor DarkGray
    Write-Host ""

    Write-Log "FATAL: $Title | $Reason" "ERROR"

    Write-Host "按回车键关闭窗口..." -ForegroundColor DarkGray
    try { Read-Host | Out-Null } catch { }
    exit 1
}


# ── 步骤状态（断点续跑的核心）────────────────────────────────

function Get-State {
    <#
      读取已完成步骤记录。

      用户装失败后往往会重复运行，没有这个记录就会把 1.2GB 重下一遍。
      文件损坏时返回空表而不是报错——宁可多做一遍，也不能卡死。
    #>
    if (-not (Test-Path $script:STATE_FILE)) {
        return @{}
    }
    try {
        $raw = Get-Content -Path $script:STATE_FILE -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return @{} }
        $obj = $raw | ConvertFrom-Json
        $ht = @{}
        foreach ($p in $obj.PSObject.Properties) {
            $ht[$p.Name] = $p.Value
        }
        return $ht
    } catch {
        Write-Log "状态文件损坏，按未完成处理: $($_.Exception.Message)" "WARN"
        return @{}
    }
}

function Set-StepDone {
    <# 标记某步骤完成，并记下时间便于排查 #>
    param([string]$Step)
    $state = Get-State
    $state[$Step] = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    try {
        if (-not (Test-Path $script:DEPLOY_DIR)) {
            New-Item -ItemType Directory -Path $script:DEPLOY_DIR -Force | Out-Null
        }
        $state | ConvertTo-Json | Set-Content -Path $script:STATE_FILE -Encoding UTF8
        Write-Log "步骤完成标记: $Step" "INFO"
    } catch {
        Write-Log "无法写入状态文件: $($_.Exception.Message)" "WARN"
    }
}

function Test-StepDone {
    <# 判断步骤是否已完成。注意：调用方仍应校验实际产物是否存在 #>
    param([string]$Step)
    $state = Get-State
    return $state.ContainsKey($Step)
}

function Clear-StepDone {
    <# 撤销某步骤标记。校验发现产物损坏时用它强制重做 #>
    param([string]$Step)
    $state = Get-State
    if ($state.ContainsKey($Step)) {
        $state.Remove($Step)
        try {
            $state | ConvertTo-Json | Set-Content -Path $script:STATE_FILE -Encoding UTF8
            Write-Log "已撤销步骤标记: $Step" "INFO"
        } catch { }
    }
}

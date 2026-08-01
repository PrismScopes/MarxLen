# ============================================================
# MarxLen 关闭脚本
#
# 由项目根目录的「关闭.bat」双击调用。
# 职责：找到自己启动的服务进程 -> 确认身份 -> 优雅停止 -> 清理 PID 文件
#
# 设计约束与 common.ps1 一致：必须兼容 Windows PowerShell 5.1，
# 面向完全不懂技术的用户，任何失败都要说清"现在该做什么"。
# ============================================================

. (Join-Path $PSScriptRoot "lib\common.ps1")

# 关闭脚本单独记日志，便于和启动过程对照排查
$script:LOG_FILE = Join-Path $script:DEPLOY_DIR "stop.log"

$PID_FILE = Join-Path $script:DEPLOY_DIR "server.pid"
$PORT     = 8000


# ── 工具函数 ────────────────────────────────────────────────

function Get-ProcCommandLine {
    <# 取进程完整命令行；取不到返回空串，让调用方按"无法确认"处理 #>
    param([int]$ProcessId)
    try {
        $p = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine) { return [string]$p.CommandLine }
    } catch {
        Write-Log "读取进程 $ProcessId 命令行失败: $($_.Exception.Message)" "WARN"
    }
    return ""
}

function Test-IsOurService {
    <#
      确认某个 PID 真的是本项目的服务，而不是碰巧同号的其他程序。

      这是本脚本最关键的一道保险。Windows 在进程退出后会把 PID 回收再分配，
      所以 server.pid 里记的号码，隔一段时间完全可能属于别的程序
      （浏览器、办公软件，甚至系统服务）。如果不核对身份就 Stop-Process，
      等于随机杀掉用户正在用的程序，可能导致他丢失未保存的工作。

      判定必须两项同时成立：
        1. 进程名是 python（我们的服务只可能由内嵌 python.exe 启动）
        2. 命令行里含 api.main 或 uvicorn（确认它跑的是本项目的服务）
      任何一项不满足，就宁可不动手，交给用户自己判断。
    #>
    param([int]$ProcessId)

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }

    if ($proc.ProcessName -notmatch "^python") {
        Write-Log "PID $ProcessId 进程名为 $($proc.ProcessName)，不是 python，判定为非本服务" "WARN"
        return $false
    }

    $cmd = Get-ProcCommandLine -ProcessId $ProcessId
    if (-not $cmd) {
        Write-Log "PID $ProcessId 命令行读取为空，无法确认身份" "WARN"
        return $false
    }
    if ($cmd -match "api\.main" -or $cmd -match "uvicorn") {
        return $true
    }

    Write-Log "PID $ProcessId 命令行不含 api.main/uvicorn，判定为非本服务" "WARN"
    return $false
}

function Get-PortOwnerPid {
    <#
      查出谁在监听指定端口，返回 PID；没人占用返回 0。
      优先 Get-NetTCPConnection，精简系统缺该命令时回退 netstat 解析。
    #>
    param([int]$Port)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if ($conns) {
                $first = @($conns)[0]
                return [int]$first.OwningProcess
            }
            return 0
        } catch {
            Write-Log "Get-NetTCPConnection 失败，回退 netstat: $($_.Exception.Message)" "WARN"
        }
    }

    try {
        $lines = netstat -ano | Select-String -Pattern "LISTENING"
        foreach ($line in $lines) {
            $cols = ($line.ToString().Trim()) -split "\s+"
            if ($cols.Count -ge 5) {
                # 用结尾匹配，避免 18000 之类被误认成 8000
                if ($cols[1] -match ":$Port$") {
                    return [int]$cols[$cols.Count - 1]
                }
            }
        }
    } catch {
        Write-Log "netstat 解析失败: $($_.Exception.Message)" "WARN"
    }
    return 0
}

function Stop-ServiceProcess {
    <#
      停止已确认身份的服务进程。

      先礼后兵：uvicorn 在收到关闭信号后会执行 lifespan 的收尾逻辑
      （关闭数据库连接、释放缓存），直接 -Force 会跳过这一步，
      有可能留下未落盘的数据或损坏的 sqlite 连接。
      所以先尝试温和方式并给 10 秒，超时才强制结束。
    #>
    param([int]$ProcessId)

    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $proc) { return $true }

    # 后台启动的服务没有窗口，CloseMainWindow 通常返回 false，
    # 这不算失败，下面的 Stop-Process（不带 -Force）才是主要手段。
    try {
        if ($proc.MainWindowHandle -ne [System.IntPtr]::Zero) {
            $null = $proc.CloseMainWindow()
        }
    } catch { }

    try {
        Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
    } catch { }

    $waited = 0
    while ($waited -lt 10) {
        Start-Sleep -Seconds 1
        $waited++
        $still = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $still) {
            Write-Log "进程 $ProcessId 已正常退出（等待 $waited 秒）" "INFO"
            return $true
        }
        Write-Host ("`r         正在等待服务退出（{0} 秒）...    " -f $waited) -NoNewline
    }
    Write-Host ""

    # 还赖着不走，只能强制
    Say-Info "服务没有响应，正在强制结束"
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    } catch { }

    $final = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($final) {
        Write-Log "强制结束后进程 $ProcessId 仍存在" "ERROR"
        return $false
    }
    Write-Log "进程 $ProcessId 已被强制结束" "INFO"
    return $true
}

function Clear-OrphanChildren {
    <#
      清理服务留下的"孤儿"子进程。

      为什么需要这一步：Python 的 multiprocessing 会派生子进程，子进程会继承
      父进程打开的端口套接字。父进程被结束后，这些子进程不会跟着退出，
      而是继续攥着 8000 端口不放。表现就是"明明关闭成功了，下次启动却说端口被占用"。

      判定孤儿的依据必须严格：命令行里带着 parent_pid=<刚结束的那个进程号>，
      这能确凿证明它是我们刚关掉那个服务的亲生子进程，而不是别人的 python。
      靠"进程名是 python"来杀是绝对不行的，用户机器上往往跑着别的 Python 程序。
    #>
    param([int]$DeadParentPid)

    $cleaned = 0
    try {
        $candidates = Get-CimInstance -ClassName Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
        foreach ($c in $candidates) {
            if (-not $c.CommandLine) { continue }
            # 两种证据任一成立即可认定：命令行里写明了父进程号，或系统记录的父进程就是它
            $byCmd = ($c.CommandLine -match "parent_pid=$DeadParentPid\b")
            $byPpid = ($c.ParentProcessId -eq $DeadParentPid)
            if ($byCmd -or $byPpid) {
                Write-Log "发现孤儿子进程 PID=$($c.ProcessId)，父进程 $DeadParentPid 已结束，一并清理" "INFO"
                try {
                    Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue
                    $cleaned++
                } catch { }
            }
        }
    } catch {
        Write-Log "清理孤儿子进程时出错: $($_.Exception.Message)" "WARN"
    }
    if ($cleaned -gt 0) {
        Start-Sleep -Seconds 1
        Say-Info "已清理 $cleaned 个残留的后台进程"
    }
    return $cleaned
}

function Test-PortReleased {
    <#
      确认端口真的被释放了。

      不能因为"进程没了"就宣布关闭成功：套接字的释放略滞后于进程退出，
      更常见的是上面说的孤儿子进程还攥着端口。
      给几秒钟轮询，避免把"看起来关了、其实没关"报告成成功——
      那会让用户在下次启动报错时完全摸不着头脑。
    #>
    param([int]$Port, [int]$TimeoutSec = 5)

    $waited = 0
    while ($waited -lt $TimeoutSec) {
        if ((Get-PortOwnerPid -Port $Port) -le 0) { return $true }
        Start-Sleep -Seconds 1
        $waited++
    }
    return ((Get-PortOwnerPid -Port $Port) -le 0)
}

function Complete-Shutdown {
    <#
      停止主进程之后的收尾：清孤儿 + 确认端口释放。
      两条关闭路线（PID 文件 / 端口兜底）共用，避免逻辑写两遍走样。
    #>
    param([int]$StoppedPid)

    if (-not (Test-PortReleased -Port $PORT -TimeoutSec 3)) {
        # 端口还被占着，极可能是孤儿子进程，按父子关系精确清理
        $null = Clear-OrphanChildren -DeadParentPid $StoppedPid

        if (-not (Test-PortReleased -Port $PORT -TimeoutSec 3)) {
            $leftPid = Get-PortOwnerPid -Port $PORT
            if ($leftPid -gt 0) {
                $leftName = "未知程序"
                try {
                    $lp = Get-Process -Id $leftPid -ErrorAction SilentlyContinue
                    if ($lp) { $leftName = [string]$lp.ProcessName }
                } catch { }
                Say-Warn "服务已停止，但端口 $PORT 仍被 $leftName（编号 $leftPid）占用"
                Say-Info "下次启动如果提示端口被占用，请再运行一次本脚本，或重启电脑。"
                Write-Log "关闭后端口仍被 PID=$leftPid ($leftName) 占用" "WARN"
            }
        }
    }
}

function Remove-PidFile {
    try {
        if (Test-Path $PID_FILE) {
            Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
            Write-Log "已删除 PID 文件" "INFO"
        }
    } catch {
        Write-Log "删除 PID 文件失败: $($_.Exception.Message)" "WARN"
    }
}

function Show-Closed {
    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host "  服务已关闭" -ForegroundColor Green
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  下次使用时，双击 启动.bat 即可重新打开。" -ForegroundColor White
    Write-Host "  本窗口可以关掉了。" -ForegroundColor DarkGray
    Write-Host ""
}


# ── 主流程 ──────────────────────────────────────────────────

try {

    Say-Step "MarxLen 关闭中"
    Write-Log "===== 关闭脚本开始 =====" "INFO"

    $stopped = $false

    # ---------- 路线一：按 PID 文件精确停止 ----------
    if (Test-Path $PID_FILE) {

        $pidText = ""
        try {
            $pidText = (Get-Content -Path $PID_FILE -Raw -ErrorAction SilentlyContinue)
            if ($pidText) { $pidText = $pidText.Trim() }
        } catch { }

        $targetPid = 0
        if ($pidText -match "^\d+$") { $targetPid = [int]$pidText }

        if ($targetPid -le 0) {
            # PID 文件内容不可信，当作没有它，走端口兜底
            Say-Info "服务记录文件内容异常，改用其他方式检查"
            Write-Log "PID 文件内容无法解析: '$pidText'" "WARN"
            Remove-PidFile
        } else {
            $running = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
            if (-not $running) {
                Say-Info "记录中的服务已经不在运行了"
                Write-Log "PID $targetPid 已不存在" "INFO"
                Remove-PidFile
            } elseif (-not (Test-IsOurService -ProcessId $targetPid)) {
                # 关键分支：号码还在，但主人换了。绝不能动手。
                Say-Warn "服务记录已过期，记录中的编号现在属于其他程序，已跳过（不会误关你的其他程序）"
                Write-Log "PID $targetPid 已被其他程序复用，拒绝结束" "WARN"
                Remove-PidFile
            } else {
                Say "正在关闭服务..."
                if (Stop-ServiceProcess -ProcessId $targetPid) {
                    Complete-Shutdown -StoppedPid $targetPid
                    Remove-PidFile
                    Say-Ok "服务已停止"
                    $stopped = $true
                } else {
                    Stop-WithError -Title "服务没能关闭" `
                        -Reason "已尝试强制结束编号为 $targetPid 的服务进程，但它仍在运行。通常是权限不足造成的。" `
                        -Solutions @(
                            "在 关闭.bat 上点右键，选择「以管理员身份运行」，再试一次",
                            "或者按 Ctrl+Shift+Esc 打开任务管理器，在「详细信息」标签里找到 PID 为 $targetPid 的 python.exe，右键结束任务",
                            "重启电脑也可以彻底关闭它"
                        )
                }
            }
        }
    } else {
        Say-Info "没有找到服务运行记录"
        Write-Log "PID 文件不存在" "INFO"
    }

    # ---------- 路线二：端口兜底 ----------
    # PID 文件可能被误删，或服务是用其他方式启动的（比如手动命令行），
    # 这时端口上仍有残留进程占着 8000，不清掉下次启动会报"端口被占用"。
    if (-not $stopped) {

        $ownerPid = Get-PortOwnerPid -Port $PORT

        if ($ownerPid -le 0) {
            Say-Ok "服务当前没有在运行"
            Write-Host ""
            Write-Host "  如需使用，双击 启动.bat 即可。" -ForegroundColor DarkGray
            Write-Host ""
            Write-Log "端口 $PORT 无占用，无需处理" "INFO"
            Write-Log "===== 关闭脚本结束 =====" "INFO"
            exit 0
        }

        if (Test-IsOurService -ProcessId $ownerPid) {
            Say-Info "发现一个仍在运行的服务（编号 $ownerPid）"
            Say "正在关闭服务..."
            if (Stop-ServiceProcess -ProcessId $ownerPid) {
                Complete-Shutdown -StoppedPid $ownerPid
                Remove-PidFile
                Say-Ok "服务已停止"
                $stopped = $true
            } else {
                Stop-WithError -Title "服务没能关闭" `
                    -Reason "已尝试强制结束编号为 $ownerPid 的服务进程，但它仍在运行。通常是权限不足造成的。" `
                    -Solutions @(
                        "在 关闭.bat 上点右键，选择「以管理员身份运行」，再试一次",
                        "或者按 Ctrl+Shift+Esc 打开任务管理器，在「详细信息」标签里找到 PID 为 $ownerPid 的 python.exe，右键结束任务",
                        "重启电脑也可以彻底关闭它"
                    )
            }
        } else {
            # 占用者身份不明，可能是用户自己的其他程序。
            # 结束别人的进程属于不可逆操作，必须让用户拍板，不能替他决定。
            $ownerName = "未知程序"
            try {
                $op = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
                if ($op) { $ownerName = [string]$op.ProcessName }
            } catch { }

            Write-Host ""
            Say-Warn "端口 $PORT 被另一个程序占用：$ownerName（编号 $ownerPid）"
            Say-Info "它看起来不是本程序的服务，可能是你电脑上的其他软件。"
            Say-Info "如果不确定它是什么，建议选 N，先去任务管理器里看一眼。"
            Write-Host ""

            $answer = ""
            try {
                $answer = Read-Host "要强制结束这个程序吗？结束后它未保存的内容会丢失。(Y=结束 / N=不动它)"
            } catch { }

            if ($answer -and $answer.Trim().ToUpper().StartsWith("Y")) {
                Write-Log "用户确认强制结束占用进程 PID=$ownerPid ($ownerName)" "WARN"
                try {
                    Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 2
                } catch { }

                $still = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
                if ($still) {
                    Stop-WithError -Title "没能结束该程序" `
                        -Reason "尝试结束 $ownerName（编号 $ownerPid）失败，通常是权限不足。" `
                        -Solutions @(
                            "在 关闭.bat 上点右键，选择「以管理员身份运行」，再试一次",
                            "或者按 Ctrl+Shift+Esc 打开任务管理器，找到 PID 为 $ownerPid 的 $ownerName，右键结束任务"
                        )
                }
                Say-Ok "已结束 $ownerName，端口 $PORT 现在空闲了"
                Remove-PidFile
                $stopped = $true
            } else {
                Say-Info "已保持原样，没有结束任何程序。"
                Say-Info "本程序的服务当前并未运行，可以放心。"
                Write-Host ""
                Write-Log "用户拒绝结束占用进程 PID=$ownerPid" "INFO"
                Write-Log "===== 关闭脚本结束 =====" "INFO"
                exit 0
            }
        }
    }

    Show-Closed
    Write-Log "===== 关闭脚本结束 =====" "INFO"
    exit 0

} catch {
    # 兜底：不把异常堆栈甩给用户，转成能照着做的处理办法
    Write-Log "未预期的异常: $($_.Exception.Message)" "ERROR"
    Write-Log "异常位置: $($_.InvocationInfo.PositionMessage)" "ERROR"
    Stop-WithError -Title "关闭过程中出现意外问题" `
        -Reason "程序遇到了没有预料到的情况，已经停下来了。服务可能仍在运行。" `
        -Solutions @(
            "按 Ctrl+Shift+Esc 打开任务管理器，切到「详细信息」标签，找到 python.exe 并右键结束任务",
            "重启电脑可以彻底关闭所有后台服务",
            "把 deploy 文件夹里的 stop.log 发给作者，里面记录了详细信息"
        )
}

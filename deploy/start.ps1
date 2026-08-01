# ============================================================
# MarxLen 启动脚本
#
# 由项目根目录的「启动.bat」双击调用。
# 职责：启动前体检 -> 端口检查 -> 后台拉起服务 -> 等待就绪 -> 开浏览器
#
# 设计约束与 common.ps1 一致：必须兼容 Windows PowerShell 5.1，
# 面向完全不懂技术的用户，任何失败都要说清"现在该做什么"。
# ============================================================

# 引入公共库（日志、错误中断、路径变量）
. (Join-Path $PSScriptRoot "lib\common.ps1")

# 启动脚本的日志与安装日志分开，避免把安装记录冲乱
$script:LOG_FILE = Join-Path $script:DEPLOY_DIR "start.log"

# ── 本脚本用到的路径 ────────────────────────────────────────
$PY_EXE      = Join-Path $script:ROOT "python-embed\python\python.exe"
$RAG_DIR     = Join-Path $script:ROOT "rag"
$ENV_FILE    = Join-Path $RAG_DIR ".env"
$PID_FILE    = Join-Path $script:DEPLOY_DIR "server.pid"

# stdout 与 stderr 必须分成两个文件：Start-Process 不允许两路重定向
# 指向同一个文件，会直接报错启动失败。
# uvicorn 与 Python logging 默认写 stderr，所以 server.log 才是主日志。
$LOG_ERR     = Join-Path $script:DEPLOY_DIR "server.log"
$LOG_OUT     = Join-Path $script:DEPLOY_DIR "server.out.log"

$PORT        = 8000
$HEALTH_URL  = "http://127.0.0.1:$PORT/api/health"
$OPEN_URL    = "http://localhost:$PORT"

# 索引文件体积下限。
# 不做精确匹配是因为版本迭代会让体积小幅浮动，误报反而吓到用户；
# 但必须挡住两类事故：仓库用 Git LFS 时下下来的"指针文件"（几百字节），
# 以及下载中断留下的残缺文件。100MB 这条线两者都能挡住。
$MIN_INDEX_SIZE = 100MB

$INDEX_FILES = @(
    @{ Path = (Join-Path $RAG_DIR "documents.db");    Name = "原文数据库";   Approx = "约 282 MB" },
    @{ Path = (Join-Path $RAG_DIR "faiss_index.idx"); Name = "向量索引";     Approx = "约 637 MB" },
    @{ Path = (Join-Path $RAG_DIR "bm25_index.pkl");  Name = "关键词索引";   Approx = "约 245 MB" }
)

# 几乎所有失败都指向"没装好"，统一用这句收尾，省得用户到处找入口
$HINT_INSTALL = "请先双击运行 安装.bat，等它全部跑完再来启动"


# ── 工具函数 ────────────────────────────────────────────────

function Get-ProcCommandLine {
    <#
      取进程的完整命令行。

      为什么不用 Get-Process：它拿不到命令行参数，而我们恰恰要靠
      参数里有没有 api.main 来判断"这个 python 是不是我们的服务"。
      拿不到时返回空字符串，让调用方按"无法确认"处理，绝不猜。
    #>
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
    <# 命令行里同时出现 api.main，才认定是本项目的服务进程 #>
    param([int]$ProcessId)
    $cmd = Get-ProcCommandLine -ProcessId $ProcessId
    if (-not $cmd) { return $false }
    return ($cmd -match "api\.main")
}

function Get-PortOwnerPid {
    <#
      查出谁在监听指定端口，返回 PID；没人占用返回 0。

      优先用 Get-NetTCPConnection，但精简版系统（部分 LTSC / Server Core）
      可能没有 NetTCPIP 模块，所以留了 netstat 解析这条后路，
      保证在任何一台 Windows 上都能得出结论。
    #>
    param([int]$Port)

    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            if ($conns) {
                # 可能返回多条（IPv4/IPv6 各一条），取第一条即可
                $first = @($conns)[0]
                return [int]$first.OwningProcess
            }
            return 0
        } catch {
            Write-Log "Get-NetTCPConnection 失败，回退 netstat: $($_.Exception.Message)" "WARN"
        }
    }

    # 回退方案：解析 netstat -ano 的 LISTENING 行
    try {
        $lines = netstat -ano | Select-String -Pattern "LISTENING"
        foreach ($line in $lines) {
            $text = $line.ToString().Trim()
            # 形如：TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    12345
            $cols = $text -split "\s+"
            if ($cols.Count -ge 5) {
                $local = $cols[1]
                # 用 EndsWith 判断，避免把 18000、80001 之类误判成 8000
                if ($local -match ":$Port$") {
                    return [int]$cols[$cols.Count - 1]
                }
            }
        }
    } catch {
        Write-Log "netstat 解析失败: $($_.Exception.Message)" "WARN"
    }
    return 0
}

function Get-ProcNameSafe {
    <# 取进程名，取不到就返回"未知程序"，不让脚本因此中断 #>
    param([int]$ProcessId)
    try {
        $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($p) { return [string]$p.ProcessName }
    } catch { }
    return "未知程序"
}

function Show-ServerLogTail {
    <#
      启动失败时把日志尾部摊开给用户看。

      普通用户看不懂 Python 堆栈，但把这几行截图发给作者就能定位问题，
      比只说一句"启动失败"有用得多。
    #>
    param([int]$Lines = 20)

    foreach ($f in @($LOG_ERR, $LOG_OUT)) {
        if (-not (Test-Path $f)) { continue }
        try {
            $content = Get-Content -Path $f -Tail $Lines -ErrorAction SilentlyContinue
            if ($content) {
                Write-Host ""
                Write-Host "--- $(Split-Path -Leaf $f) 最后 $Lines 行 ---" -ForegroundColor DarkGray
                foreach ($l in $content) { Write-Host "  $l" -ForegroundColor DarkGray }
            }
        } catch { }
    }
    Write-Host ""
}


# ── 主流程 ──────────────────────────────────────────────────

try {

    Say-Step "MarxLen 启动中"
    Write-Log "===== 启动脚本开始 =====" "INFO"

    # ---------- 第 1 步：启动前体检 ----------
    Say-Step "第 1 步 / 共 4 步：检查程序是否装好"

    # (1) 内嵌 Python
    if (-not (Test-Path $PY_EXE)) {
        Stop-WithError -Title "找不到程序运行环境" `
            -Reason "缺少运行本程序所需的 Python 环境（应位于 python-embed 文件夹内），说明安装还没有完成。" `
            -Solutions @(
                $HINT_INSTALL,
                "如果安装过程中途出错或被关掉，重新双击 安装.bat 即可接着装，不会从头再来",
                "若反复失败，请把 deploy 文件夹里的 install.log 发给作者"
            )
    }
    Say-Ok "运行环境已就绪"

    # (2) .env 与 API Key
    if (-not (Test-Path $ENV_FILE)) {
        Stop-WithError -Title "还没有填写 API 密钥" `
            -Reason "缺少配置文件 rag\.env，程序不知道该用哪个账号去调用大模型。" `
            -Solutions @(
                $HINT_INSTALL,
                "安装过程中会提示你填写 API 密钥，请按提示填完",
                "如果你已经有密钥，可以把 rag\.env.example 复制一份改名为 rag\.env，再把里面的 sk-your-api-key 换成你自己的密钥"
            )
    }

    $apiKey = ""
    try {
        # 逐行找 OPENAI_API_KEY=，忽略以 # 开头的注释行
        $envLines = Get-Content -Path $ENV_FILE -Encoding UTF8 -ErrorAction SilentlyContinue
        foreach ($line in $envLines) {
            $t = $line.Trim()
            if ($t.StartsWith("#")) { continue }
            if ($t -match "^OPENAI_API_KEY\s*=\s*(.*)$") {
                $apiKey = $Matches[1].Trim().Trim('"').Trim("'")
                break
            }
        }
    } catch {
        Write-Log "读取 .env 失败: $($_.Exception.Message)" "WARN"
    }

    if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey -eq "sk-your-api-key") {
        Stop-WithError -Title "API 密钥还没填" `
            -Reason "配置文件 rag\.env 里的 OPENAI_API_KEY 还是示例内容，不是你自己的密钥。没有它程序无法回答问题。" `
            -Solutions @(
                "用记事本打开 rag\.env 文件",
                "找到以 OPENAI_API_KEY= 开头的那一行",
                "把等号后面的 sk-your-api-key 替换成你申请到的密钥（形如 sk-xxxxxxxx），保存后重新双击 启动.bat",
                "还没有密钥的话，可到 https://platform.deepseek.com 注册后在控制台创建"
            )
    }
    Say-Ok "API 密钥已配置"

    # (3) 三个索引文件
    foreach ($item in $INDEX_FILES) {
        $path = $item.Path
        $name = $item.Name

        if (-not (Test-Path $path)) {
            Stop-WithError -Title "缺少知识库文件：$name" `
                -Reason "找不到文件 $path，程序没有资料可查。" `
                -Solutions @(
                    $HINT_INSTALL,
                    "知识库文件较大（三个文件合计约 1.2 GB），安装时需要耐心等待下载完成",
                    "如果安装时下载中断，重新运行 安装.bat 会从断点继续下载"
                )
        }

        $size = (Get-Item $path).Length
        if ($size -lt $MIN_INDEX_SIZE) {
            $sizeMB = [math]::Round($size / 1MB, 2)
            Stop-WithError -Title "知识库文件不完整：$name" `
                -Reason "文件 $path 只有 $sizeMB MB，而正常应该是 $($item.Approx)。这通常是下载没完成，或者下载到的只是一个占位文件。" `
                -Solutions @(
                    "删除这个文件：$path",
                    "然后重新双击 安装.bat，它会重新下载这个文件",
                    "下载期间请保持网络连接，不要中途关闭窗口"
                )
        }
        Say-Ok "$name 已就绪（$([math]::Round($size / 1MB, 0)) MB）"
    }

    # (4) 关键依赖能否导入
    #     装包成功不等于能用：faiss 依赖的 VC++ 运行库缺失时，
    #     只有真正 import 才会暴露，所以这里必须实跑一次。
    Say "正在检查程序组件..."
    $importOut = ""
    $importOk = $false
    try {
        $importOut = & $PY_EXE -c "import faiss, fastapi, uvicorn" 2>&1 | Out-String
        $importOk = ($LASTEXITCODE -eq 0)
    } catch {
        $importOut = $_.Exception.Message
        $importOk = $false
    }

    if (-not $importOk) {
        Write-Log "依赖导入失败: $importOut" "ERROR"
        Stop-WithError -Title "程序组件缺失或损坏" `
            -Reason "运行环境里缺少必要的组件（faiss / fastapi / uvicorn），或者组件安装不完整。" `
            -Solutions @(
                $HINT_INSTALL,
                "如果之前装过，可能是安装中途被杀毒软件拦截，请暂时关闭杀毒软件后重新运行 安装.bat",
                "重装仍失败的话，请把 deploy 文件夹里的 install.log 和 start.log 发给作者"
            )
    }
    Say-Ok "程序组件检查通过"

    # ---------- 第 2 步：端口检查 ----------
    Say-Step "第 2 步 / 共 4 步：检查端口是否可用"

    $ownerPid = Get-PortOwnerPid -Port $PORT
    if ($ownerPid -gt 0) {
        if (Test-IsOurService -ProcessId $ownerPid) {
            # 服务已经在跑，重复启动没有意义，直接把浏览器打开就是用户想要的结果
            Say-Ok "服务已经在运行中，无需重复启动"
            Say ""
            Say "正在为你打开浏览器..."
            try { Start-Process $OPEN_URL } catch {
                Say-Warn "浏览器没能自动打开，请手动在浏览器地址栏输入：$OPEN_URL"
            }
            Say ""
            Say "服务地址：$OPEN_URL"
            Say "如需关闭服务，双击 关闭.bat"
            Write-Host ""
            Write-Log "服务已在运行(PID=$ownerPid)，跳过启动" "INFO"
            exit 0
        }

        # 是别的程序占着，必须说清楚是谁，否则用户无从下手
        $ownerName = Get-ProcNameSafe -ProcessId $ownerPid
        Stop-WithError -Title "端口 $PORT 被别的程序占用了" `
            -Reason "本程序需要使用电脑的 $PORT 号端口，但它已被另一个程序占用：$ownerName（进程编号 $ownerPid）。两个程序不能同时用同一个端口。" `
            -Solutions @(
                "关闭 $ownerName 这个程序后，重新双击 启动.bat",
                "如果不确定它是什么：按 Ctrl+Shift+Esc 打开任务管理器，切到「详细信息」标签，找到 PID 为 $ownerPid 的那一项，确认无关紧要后结束它",
                "如果该程序不能关闭，请联系作者把本程序改用其他端口"
            )
    }
    Say-Ok "端口 $PORT 可用"

    # ---------- 第 3 步：启动服务 ----------
    Say-Step "第 3 步 / 共 4 步：启动服务"

    # 每次启动清空旧日志，避免用户翻到上次的报错误判
    foreach ($f in @($LOG_ERR, $LOG_OUT)) {
        try { Set-Content -Path $f -Value "" -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
    }

    # 绑定 127.0.0.1 而不是 0.0.0.0：
    #   0.0.0.0 会让局域网内任何一台机器都能访问本服务，
    #   而本服务没有登录认证，且能消耗用户自己的 API 额度。
    #   在咖啡厅、宿舍、公司这类共享网络下，这等于把接口敞开给陌生人。
    #   127.0.0.1 只允许本机访问，是单机应用的正确选择。
    # 不加 --reload：那是开发时自动重启用的，会多出一个子进程，
    #   既拖慢启动，也让 PID 记录对不上真正的服务进程。
    $argList = @(
        "-m", "uvicorn",
        "api.main:app",
        "--host", "127.0.0.1",
        "--port", "$PORT"
    )

    Say "正在启动，请稍候..."
    Write-Log "启动命令: $PY_EXE $($argList -join ' ')" "INFO"

    $proc = $null
    try {
        $proc = Start-Process -FilePath $PY_EXE `
            -ArgumentList $argList `
            -WorkingDirectory $script:ROOT `
            -RedirectStandardOutput $LOG_OUT `
            -RedirectStandardError $LOG_ERR `
            -WindowStyle Hidden `
            -PassThru
    } catch {
        Write-Log "Start-Process 失败: $($_.Exception.Message)" "ERROR"
        Stop-WithError -Title "服务没能启动起来" `
            -Reason "系统拒绝启动程序进程。常见原因是杀毒软件拦截，或者程序文件被占用。" `
            -Solutions @(
                "暂时关闭杀毒软件或把本项目文件夹加入白名单，然后重新双击 启动.bat",
                "重启电脑后再试一次",
                "仍然失败请把 deploy 文件夹里的 start.log 发给作者"
            )
    }

    if (-not $proc) {
        Stop-WithError -Title "服务没能启动起来" `
            -Reason "启动命令已执行，但没有拿到进程信息。" `
            -Solutions @(
                "重启电脑后重新双击 启动.bat",
                "把 deploy 文件夹里的 start.log 发给作者"
            )
    }

    # PID 落盘，供 关闭.bat 精确停止这个进程
    try {
        Set-Content -Path $PID_FILE -Value $proc.Id -Encoding ASCII
        Write-Log "服务进程已启动，PID=$($proc.Id)，PID 文件: $PID_FILE" "INFO"
    } catch {
        Say-Warn "进程编号没能记录下来，关闭时可能需要手动确认"
        Write-Log "写入 PID 文件失败: $($_.Exception.Message)" "WARN"
    }
    Say-Ok "服务进程已启动"

    # ---------- 第 4 步：等待就绪 ----------
    Say-Step "第 4 步 / 共 4 步：等待服务准备就绪"

    Say-Info "程序正在把约 900 MB 的知识库读进内存，首次启动通常需要 30 秒到 2 分钟。"
    Say-Info "这是正常现象，请不要关闭窗口。"
    Write-Host ""

    $timeoutSec = 180
    $intervalSec = 2
    $elapsed = 0
    $ready = $false

    while ($elapsed -lt $timeoutSec) {
        Start-Sleep -Seconds $intervalSec
        $elapsed += $intervalSec

        # 进程要是已经退了，再等下去也是白等，立刻报错省得用户干等 3 分钟
        $alive = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
        if (-not $alive) {
            Write-Host ""
            Write-Log "服务进程提前退出" "ERROR"
            Show-ServerLogTail -Lines 20
            Stop-WithError -Title "服务启动后立刻退出了" `
                -Reason "程序在启动过程中出错并自行结束。上面灰色文字是它退出前的记录。" `
                -Solutions @(
                    "先确认 rag\.env 里的 API 密钥填写正确（不能有多余的空格或引号）",
                    "确认电脑剩余内存足够（本程序需要约 2 GB 空闲内存），关掉其他大型程序后重试",
                    "把 deploy 文件夹里的 server.log 发给作者，里面有完整的出错记录"
                )
        }

        try {
            $resp = Invoke-WebRequest -Uri $HEALTH_URL -UseBasicParsing -TimeoutSec 3
            if ([int]$resp.StatusCode -eq 200) {
                $ready = $true
                break
            }
        } catch {
            # 还没起来，属于预期内，继续等
        }

        Write-Host ("`r         正在加载知识库，已等待 {0} 秒（最多等 {1} 秒）...    " -f $elapsed, $timeoutSec) -NoNewline
    }

    Write-Host ""

    if (-not $ready) {
        Write-Log "等待超时，服务未就绪" "ERROR"
        Show-ServerLogTail -Lines 20

        # 超时了但进程还活着，留着它会占住端口，下次启动反而报"端口被占用"
        try {
            $still = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
            if ($still) {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                Write-Log "已清理超时未就绪的进程 PID=$($proc.Id)" "INFO"
            }
            if (Test-Path $PID_FILE) { Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue }
        } catch { }

        Stop-WithError -Title "服务启动超时" `
            -Reason "等待了 $timeoutSec 秒，服务仍然没有准备好。上面灰色文字是它最近的运行记录。" `
            -Solutions @(
                "如果你的电脑较老或硬盘较慢，可能只是需要更久，请再试一次",
                "确认电脑剩余内存足够（本程序需要约 2 GB 空闲内存），关掉其他大型程序后重试",
                "确认知识库文件没有被杀毒软件锁定，可把项目文件夹加入杀毒软件白名单",
                "把 deploy 文件夹里的 server.log 发给作者，里面有完整的运行记录"
            )
    }

    Say-Ok "服务已准备就绪（用时 $elapsed 秒）"

    # ---------- 打开浏览器并给出收尾提示 ----------
    Say ""
    Say "正在为你打开浏览器..."
    try {
        Start-Process $OPEN_URL
    } catch {
        Say-Warn "浏览器没能自动打开，请手动在浏览器地址栏输入：$OPEN_URL"
        Write-Log "打开浏览器失败: $($_.Exception.Message)" "WARN"
    }

    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host "  启动成功" -ForegroundColor Green
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  访问地址：$OPEN_URL" -ForegroundColor White
    Write-Host "  （浏览器如果没自动打开，把上面这个地址复制到浏览器里即可）"
    Write-Host ""
    Write-Host "  关闭服务：回到项目文件夹，双击 关闭.bat" -ForegroundColor White
    Write-Host ""
    Write-Host "  本窗口现在可以关掉了，服务会在后台继续运行，不影响使用。" -ForegroundColor DarkGray
    Write-Host ""

    Write-Log "===== 启动完成，PID=$($proc.Id) =====" "INFO"
    exit 0

} catch {
    # 兜底：任何没预料到的异常都在这里转成人话，绝不把堆栈甩给用户
    Write-Log "未预期的异常: $($_.Exception.Message)" "ERROR"
    Write-Log "异常位置: $($_.InvocationInfo.PositionMessage)" "ERROR"
    Stop-WithError -Title "启动过程中出现意外问题" `
        -Reason "程序遇到了没有预料到的情况，已经停下来了。" `
        -Solutions @(
            "重启电脑后重新双击 启动.bat 试一次",
            "确认项目文件夹没有被移动过位置，也没有中文以外的特殊符号",
            "把 deploy 文件夹里的 start.log 发给作者，里面记录了详细信息"
        )
}

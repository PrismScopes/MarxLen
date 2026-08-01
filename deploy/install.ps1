﻿# ============================================================
# MarxLen 一键安装脚本
#
# 由项目根目录的「安装.bat」双击调用。
# 职责：环境自检 -> 网络检查 -> 装 Python -> 装依赖 -> 填配置
#       -> 下载知识库 -> 下载原文（可选）
#
# 设计约束与 common.ps1 / start.ps1 一致：
#   1. 必须兼容 Windows PowerShell 5.1（系统自带），不用 PS7 语法。
#   2. 面向完全不懂技术的用户，任何失败都要说清"现在该做什么"。
#
# 断点续跑的两道保险：
#   状态文件记录"哪一步做完了"，但光有标记不够——用户可能手动删过
#   文件、杀毒软件可能隔离过文件。所以每一步开头都要再看一眼实际产物
#   还在不在，产物没了就撤销标记强制重做。反过来，产物还在就绝不重下，
#   不能让用户为了一个陈旧标记把 1.2GB 再来一遍。
# ============================================================

# 引入公共库（日志、状态、错误中断）与网络库（下载、校验）
# 顺序不能反：net.ps1 里会调用 common.ps1 定义的 Say-Info
. (Join-Path $PSScriptRoot "lib\common.ps1")
. (Join-Path $PSScriptRoot "lib\net.ps1")


# ── 常量 ────────────────────────────────────────────────────

# 内嵌 Python：用 python-build-standalone 的 install_only 包。
# 不用官方 embeddable zip 的原因：那个精简版没有 pip、没有 site-packages
# 机制，装 faiss 这类带二进制的包会各种出错。
$PY_URL     = "https://github.com/astral-sh/python-build-standalone/releases/download/20260728/cpython-3.12.13+20260728-x86_64-pc-windows-msvc-install_only.tar.gz"
$PY_PKG     = Join-Path $script:CACHE_DIR "python-3.12.13-x86_64-windows.tar.gz"
$PY_EXE     = Join-Path $script:PY_DIR "python\python.exe"
$TAR_EXE    = Join-Path $env:SystemRoot "System32\tar.exe"

$RAG_DIR    = Join-Path $script:ROOT "rag"
$WW_DIR     = Join-Path $script:ROOT "ww"
$ENV_FILE   = Join-Path $RAG_DIR ".env"
$ENV_SAMPLE = Join-Path $RAG_DIR ".env.example"
$PYPROJECT  = Join-Path $RAG_DIR "pyproject.toml"

# 知识库三件套。Size 只用于给用户显示预估进度，不做相等校验：
# 版本迭代会让体积小幅浮动，用精确值反而会把好文件判成坏文件。
$INDEX_BASE = "https://github.com/PrismScopes/MarxLen/releases/download/data-v1"
$INDEX_FILES = @(
    @{ File = "documents.db";    Name = "原文数据库"; Size = 295475200 },
    @{ File = "faiss_index.idx"; Name = "向量索引";   Size = 668180000 },
    @{ File = "bm25_index.pkl";  Name = "关键词索引"; Size = 257640000 }
)

# 体积下限。要挡住两类事故：仓库用 Git LFS 时下到的"指针文件"（几百字节），
# 以及下载中断留下的残缺文件。100MB 这条线两者都能挡住，
# 且与 start.ps1 的启动前体检保持一致。
$MIN_INDEX_SIZE = 100MB

$CORPUS_URL = "https://github.com/PrismScopes/marxist-classics-markdown/archive/refs/heads/main.zip"
$CORPUS_ZIP = Join-Path $script:CACHE_DIR "marxist-classics-markdown.zip"
$CORPUS_SUB = "marxist-classics-markdown-main"

# 备用镜像源。默认官方源在国内经常慢到超时，失败后自动换这个重试一次。
$PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# .env 模板里的占位符。用户没改就等于没填，必须拦下来，
# 否则装完一启动照样报错，用户还以为是程序坏了。
$PLACEHOLDER_KEYS = @("sk-your-api-key", "sk-your-embedding-api-key")

$TOTAL_STEPS = 7


# ── 工具函数 ────────────────────────────────────────────────

function Invoke-Native {
    <#
      调用外部程序并捕获输出、退出码。

      为什么要单独包一层：common.ps1 设了 $ErrorActionPreference = "Stop"，
      在这个设置下，外部程序只要往 stderr 写一个字，PowerShell 5.1 就会把它
      当成终止性错误抛出来（NativeCommandError）——可 pip、tar 往 stderr 写
      提示信息是家常便饭，那样脚本会毫无道理地中断。
      所以这里临时把它降成 Continue，只用退出码判断成败，这才是可靠的。
    #>
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [string]$WorkDir = ""
    )

    $prevPref = $ErrorActionPreference
    $prevCwd = (Get-Location).Path
    $out = ""
    $code = -1

    try {
        $ErrorActionPreference = "Continue"
        if ($WorkDir -and (Test-Path $WorkDir)) { Set-Location -Path $WorkDir }
        $out = (& $Exe @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } catch {
        # 程序根本没跑起来（文件不存在、被杀毒拦截）也走这里，
        # 统一按"失败"返回，由调用方决定怎么向用户解释
        $out = $_.Exception.Message
        $code = -1
    } finally {
        Set-Location -Path $prevCwd
        $ErrorActionPreference = $prevPref
    }

    return [PSCustomObject]@{ ExitCode = $code; Output = $out }
}

function Invoke-NativeStreaming {
    <#
      同上，但把输出实时打到屏幕上。

      专给 pip 用：装依赖要 3-10 分钟，屏幕上一动不动的话，
      用户会以为卡死了直接把窗口关掉，前功尽弃。
    #>
    param(
        [string]$Exe,
        [string[]]$Arguments
    )

    $prevPref = $ErrorActionPreference
    $code = -1
    try {
        $ErrorActionPreference = "Continue"
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            if ($line.Trim()) {
                Write-Host "         $line" -ForegroundColor DarkGray
                Write-Log $line "PIP"
            }
        }
        $code = $LASTEXITCODE
    } catch {
        Write-Log "执行失败: $($_.Exception.Message)" "ERROR"
        $code = -1
    } finally {
        $ErrorActionPreference = $prevPref
    }
    return $code
}

function Get-UrlStatusCode {
    <#
      只取 HTTP 状态码，不下载内容。

      用途是提前识别 404：知识库那个 Release 可能还没发布，
      直接调 Invoke-Download 的话会白白重试三轮、等上十几秒，
      最后还给不出"到底是没网还是文件不存在"这个关键区别。
      返回 0 表示压根没连上（断网、DNS 失败、超时）。
    #>
    param([string]$Url)
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method = "HEAD"
        $req.UserAgent = "MarxLen-Installer"
        $req.Timeout = 20000
        $req.AllowAutoRedirect = $true
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return $code
    } catch {
        try {
            $r = $_.Exception.Response
            if ($r) { return [int]$r.StatusCode }
        } catch { }
        return 0
    }
}

function Get-EnvValue {
    <# 从 .env 里读某个键的值。读不到返回空串，注释行直接跳过 #>
    param(
        [string]$Path,
        [string]$Key
    )
    if (-not (Test-Path $Path)) { return "" }
    try {
        $lines = Get-Content -Path $Path -Encoding UTF8
        foreach ($line in $lines) {
            $t = "$line".Trim()
            if ($t.StartsWith("#")) { continue }
            if ($t -match "^$Key\s*=\s*(.*)$") {
                return $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    } catch {
        Write-Log "读取 $Path 的 $Key 失败: $($_.Exception.Message)" "WARN"
    }
    return ""
}

function Test-KeyFilled {
    <# 密钥是否算"填好了"：非空、且不是模板里的占位符。不联网验证真假 #>
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    foreach ($ph in $PLACEHOLDER_KEYS) {
        if ($Value -eq $ph) { return $false }
    }
    return $true
}

function Get-PyProjectDeps {
    <#
      从 pyproject.toml 的 dependencies 数组里读出依赖清单。

      为什么要读文件而不是把包名写死在脚本里：依赖清单以后一定会变，
      写死就意味着改一处漏一处，用户装出来的环境和项目要求对不上，
      而这种错要等到启动时才暴露，排查成本极高。
    #>
    param([string]$Path)

    $raw = Get-Content -Path $Path -Raw -Encoding UTF8
    # 匹配 dependencies = [ ... ] 这一段（跨行）
    $m = [regex]::Match($raw, 'dependencies\s*=\s*\[(?<body>[^\]]*)\]')
    if (-not $m.Success) { return @() }

    $deps = @()
    foreach ($q in [regex]::Matches($m.Groups["body"].Value, '"([^"]+)"')) {
        $item = $q.Groups[1].Value.Trim()
        if ($item) { $deps += $item }
    }
    return $deps
}


# ── 主流程 ──────────────────────────────────────────────────

try {

    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Host "  MarxLen 马列经典著作智能问答系统 - 一键安装" -ForegroundColor Cyan
    Write-Host "===============================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  安装过程全自动，中途只需要你做一件事：填写 API 密钥。"
    Write-Host "  全程约需 20-60 分钟，主要时间花在下载上，取决于你的网速。"
    Write-Host ""
    Write-Host "  安装中途可以随时关闭窗口，下次重新双击 安装.bat" -ForegroundColor DarkGray
    Write-Host "  会从上次断掉的地方接着装，已经下好的东西不会重下。" -ForegroundColor DarkGray
    Write-Host ""

    Write-Log "===== 安装脚本开始 =====" "INFO"
    Write-Log "项目根目录: $script:ROOT" "INFO"

    # 缓存目录用来放下载的安装包，集中在 deploy 下便于事后整体清理
    if (-not (Test-Path $script:CACHE_DIR)) {
        New-Item -ItemType Directory -Path $script:CACHE_DIR -Force | Out-Null
    }


    # ==========================================================
    # 第 1 步：环境自检
    # ==========================================================
    Say-Step "第 1 步 / 共 $TOTAL_STEPS 步：检查你的电脑能不能装"

    # 这一步纯检查、不产生任何文件，所以每次都重新跑一遍。
    # 用户完全可能换台机器、或者把硬盘塞满之后再来重试。

    # (1) 系统自带 tar.exe —— Windows 10 1803 才开始有它。
    #     与其去解析版本号（家庭版/LTSC/预览版的版本号五花八门），
    #     不如直接看这个文件在不在，我们要的本来就是它。
    if (-not (Test-Path $TAR_EXE)) {
        Stop-WithError -Title "你的 Windows 版本太旧了" `
            -Reason "本程序需要 Windows 10（2018 年 4 月更新版）或更新的系统。你的系统里缺少解压安装包所必需的组件。" `
            -Solutions @(
                "打开「设置」-「更新和安全」-「Windows 更新」，把系统更新到最新版本后重试",
                "如果你用的是 Windows 7 或 Windows 8，需要先升级到 Windows 10 或 11",
                "如果系统无法升级，可以联系作者索取适用于旧系统的安装方式"
            )
    }
    Say-Ok "系统版本符合要求"

    # (2) CPU 架构必须是 64 位 x64。
    #     faiss 这个核心组件只有 x86_64 的安装包，ARM 芯片（如骁龙笔记本）
    #     和 32 位系统上装不了，早点说清楚，别让用户下完 1.2GB 才发现。
    $arch = "$env:PROCESSOR_ARCHITECTURE"
    if ("$env:PROCESSOR_ARCHITEW6432") { $arch = "$env:PROCESSOR_ARCHITEW6432" }
    Write-Log "CPU 架构: $arch" "INFO"

    if ($arch -like "ARM*") {
        Stop-WithError -Title "你的电脑处理器不受支持" `
            -Reason "本程序的核心检索组件只发布了适用于 Intel / AMD 处理器的版本，而你的电脑用的是 ARM 处理器（常见于骁龙、Surface Pro X 等机型）。" `
            -Solutions @(
                "换一台 Intel 或 AMD 处理器的 Windows 电脑安装",
                "或者联系作者，询问是否有适用于 ARM 的版本"
            )
    }
    if ($arch -ne "AMD64") {
        Stop-WithError -Title "你的 Windows 是 32 位版本" `
            -Reason "本程序需要 64 位 Windows 才能运行（32 位系统最多只能用 4GB 内存，装不下本程序需要加载的知识库）。" `
            -Solutions @(
                "在「设置」-「系统」-「关于」里确认「系统类型」，需要是「64 位操作系统」",
                "如果你的电脑硬件支持 64 位，需要重装 64 位版本的 Windows",
                "或者换一台 64 位 Windows 电脑安装"
            )
    }
    Say-Ok "处理器架构符合要求（64 位）"

    # (3) 磁盘剩余空间。
    #     6GB = 索引 1.2G + 语料 152M + 依赖约 1G + Python 44M
    #           + 解压过程中安装包与解压结果同时存在的临时占用 + 余量
    $needBytes = 6GB
    $freeBytes = -1
    $driveName = "此磁盘"
    try {
        # 用 DriveInfo 而不是 Get-Item 的 PSDrive 属性：后者在某些 provider
        # 路径下拿不到 Free，且严格模式下会直接抛属性不存在的错。
        $pathRoot = [System.IO.Path]::GetPathRoot($script:ROOT)
        $drive = New-Object System.IO.DriveInfo($pathRoot)
        $freeBytes = [int64]$drive.AvailableFreeSpace
        $driveName = $pathRoot.TrimEnd('\')
    } catch {
        Write-Log "无法读取磁盘剩余空间: $($_.Exception.Message)" "WARN"
    }

    if ($freeBytes -lt 0) {
        # 网络盘、映射盘可能读不出剩余空间。读不出就别拦着用户，
        # 真装满了后面下载自然会报错，那时的提示同样清楚。
        Say-Warn "没能读出磁盘剩余空间，无法提前检查。请自行确认至少有 6 GB 可用空间。"
    } elseif ($freeBytes -lt $needBytes) {
        $lack = $needBytes - $freeBytes
        Stop-WithError -Title "磁盘空间不够" `
            -Reason "安装本程序需要约 6 GB 空间，而 $driveName 现在只剩 $(Format-Size $freeBytes)，还差 $(Format-Size $lack)。" `
            -Solutions @(
                "清理 $driveName 上的文件，腾出至少 $(Format-Size $lack) 的空间后重新运行安装",
                "在「设置」-「系统」-「存储」里可以看到什么占了空间，也可以用「存储感知」一键清理",
                "或者把整个项目文件夹整体移动到空间充足的其他磁盘（比如 D 盘），再双击那里的 安装.bat"
            )
    } else {
        Say-Ok "磁盘空间充足（可用 $(Format-Size $freeBytes)，需要约 6 GB）"
    }

    # (4) 路径里的空格和中文只警告不拦。
    #     绝大多数情况下没问题（脚本里所有路径都加了引号），
    #     但个别 Python 包的构建脚本对它敏感，出问题时这条提醒能省很多排查时间。
    if ($script:ROOT -match '\s') {
        Say-Warn "项目所在路径含有空格：$script:ROOT"
        Say-Info "通常没问题，但万一安装出错，可以把项目文件夹移到没有空格的路径下再试，例如 D:\MarxLen"
    }
    if ($script:ROOT -match '[^\x00-\x7F]') {
        Say-Warn "项目所在路径含有中文：$script:ROOT"
        Say-Info "通常没问题，但万一安装出错，可以把项目文件夹移到全英文路径下再试，例如 D:\MarxLen"
    }

    # (5) 写权限。
    #     装在 C:\Program Files 下、或者从只读介质运行时会失败。
    #     用试写真文件来判断，比查 ACL 权限位准确得多。
    $probe = Join-Path $script:DEPLOY_DIR ("write_probe_" + [guid]::NewGuid().ToString("N") + ".tmp")
    try {
        Set-Content -Path $probe -Value "probe" -Encoding ASCII
        Remove-Item -Path $probe -Force
        Say-Ok "文件夹可正常读写"
    } catch {
        Write-Log "写权限探测失败: $($_.Exception.Message)" "ERROR"
        Stop-WithError -Title "没有权限往这个文件夹里写文件" `
            -Reason "安装程序需要在项目文件夹里创建文件，但系统拒绝了。常见原因是项目放在了「C:\Program Files」这类受系统保护的位置，或者放在了只读的 U 盘 / 光盘上。" `
            -Solutions @(
                "把整个项目文件夹移动到普通位置，比如 D:\MarxLen 或桌面，然后双击那里的 安装.bat",
                "或者右键点击 安装.bat，选择「以管理员身份运行」",
                "如果项目在 U 盘上，请先复制到电脑硬盘里再安装"
            )
    }


    # ==========================================================
    # 第 2 步：网络连通性
    # ==========================================================
    Say-Step "第 2 步 / 共 $TOTAL_STEPS 步：检查网络是否通畅"

    Say "正在测试网络连接，请稍候..."

    # pypi 是装依赖的唯一来源，不通就没法继续，必须拦下。
    if (-not (Test-Network -Url "https://pypi.org")) {
        Stop-WithError -Title "连不上软件源服务器" `
            -Reason "安装程序需要从 pypi.org 下载运行所需的组件，但现在连不上它。" `
            -Solutions @(
                "确认电脑已经联网：打开浏览器随便访问一个网站试试",
                "如果你在公司或学校的网络里，可能是防火墙拦截了，试试换成手机热点",
                "如果你开着 VPN 或代理软件，试试关掉它再运行；反之，如果本来就上不了外网，试试打开它",
                "确认无误后重新双击 安装.bat"
            )
    }
    Say-Ok "软件源服务器连接正常"

    # github 只警告不拦：它是知识库和原文的下载源，但此刻的探测结果
    # 未必代表几分钟后真正下载时的情况（GitHub 在国内时通时断很常见）。
    # 真到下载那步失败了再报错，信息更准确、更有针对性。
    if (-not (Test-Network -Url "https://github.com")) {
        Say-Warn "暂时连不上 GitHub（知识库文件的下载来源）"
        Say-Info "先继续往下装，等真正下载时如果还是不通，会再给你具体提示。"
        Say-Info "如果你有加速器或代理软件，建议现在就打开。"
    } else {
        Say-Ok "知识库服务器连接正常"
    }

    Set-StepDone "network"


    # ==========================================================
    # 第 3 步：安装内嵌 Python
    # ==========================================================
    Say-Step "第 3 步 / 共 $TOTAL_STEPS 步：安装程序运行环境"

    # 产物校验：光看状态标记不够，python.exe 可能被杀毒软件隔离了。
    # 只有"文件在 + 版本对"才算真的装好，否则撤标记重装。
    $pyReady = $false
    if (Test-Path $PY_EXE) {
        $ver = Invoke-Native -Exe $PY_EXE -Arguments @("--version")
        if ($ver.ExitCode -eq 0 -and $ver.Output -match "3\.12") {
            $pyReady = $true
        } else {
            Write-Log "已存在的 Python 不可用: $($ver.Output)" "WARN"
        }
    }

    if ($pyReady -and (Test-StepDone "python")) {
        Say-Skip "运行环境已安装，跳过"
    } else {
        if (Test-StepDone "python") {
            # 标记在、东西没了：典型是被杀毒软件清理过
            Clear-StepDone "python"
            Say-Warn "上次装好的运行环境不见了或已损坏，将重新安装"
        }

        # 下载安装包（约 44MB）。Invoke-Download 自带断点续传，
        # 上次下到一半的 .part 文件会被接着用，不会白下。
        if (-not (Test-FileValid -Path $PY_PKG -MinSize 10MB)) {
            Say "正在下载运行环境（约 44 MB）..."
            $ok = Invoke-Download -Url $PY_URL -OutFile $PY_PKG -Description "运行环境"
            if (-not $ok) {
                Stop-WithError -Title "运行环境下载失败" `
                    -Reason "从 GitHub 下载 Python 运行环境时反复失败。国内访问 GitHub 不稳定是最常见的原因。" `
                    -Solutions @(
                        "过几分钟重新双击 安装.bat 再试一次，已下载的部分会被保留，不会从头下",
                        "如果你有网络加速器或代理软件，打开它之后再重试",
                        "换个网络环境试试，比如切换到手机热点",
                        "实在下不动，可以联系作者索取离线安装包"
                    )
            }
        } else {
            Say-Skip "安装包已下载过，直接使用"
        }

        # 解压前先清掉残留：上次解压到一半留下的半截文件会让
        # 这次解压出来的环境处于新旧混杂状态，比重来一遍更难查。
        $pySub = Join-Path $script:PY_DIR "python"
        if (Test-Path $pySub) {
            try { Remove-Item -Path $pySub -Recurse -Force } catch {
                Write-Log "清理旧运行环境失败: $($_.Exception.Message)" "WARN"
            }
        }
        if (-not (Test-Path $script:PY_DIR)) {
            New-Item -ItemType Directory -Path $script:PY_DIR -Force | Out-Null
        }

        Say "正在解压运行环境，请稍候（约 1 分钟）..."
        # 用系统自带的 tar 解压 tar.gz，免去再引入第三方解压工具
        $tar = Invoke-Native -Exe $TAR_EXE -Arguments @("-xzf", $PY_PKG, "-C", $script:PY_DIR)
        if ($tar.ExitCode -ne 0) {
            Write-Log "tar 解压失败: $($tar.Output)" "ERROR"
            Stop-WithError -Title "运行环境解压失败" `
                -Reason "安装包解压时出错。多半是下载的安装包不完整，或者杀毒软件在解压过程中拦截了文件。" `
                -Solutions @(
                    "暂时关闭杀毒软件（或把项目文件夹加入白名单），然后重新双击 安装.bat",
                    "删除 deploy\cache 文件夹后重新运行安装，让它重新下载安装包",
                    "确认磁盘空间充足（解压需要约 200 MB 临时空间）",
                    "仍然失败请把 deploy\install.log 发给作者"
                )
        }

        if (-not (Test-Path $PY_EXE)) {
            Stop-WithError -Title "运行环境安装不完整" `
                -Reason "安装包解压完成了，但没有找到应该出现的程序文件。可能是杀毒软件在解压后立刻把它删除或隔离了。" `
                -Solutions @(
                    "打开杀毒软件的「隔离区」或「病毒查杀记录」，如果看到 python.exe 被隔离，请恢复它并加入信任",
                    "把项目文件夹整个加入杀毒软件白名单后，重新双击 安装.bat",
                    "Windows 自带的「病毒和威胁防护」里也有排除项设置，可以把项目文件夹加进去"
                )
        }

        # 装完必须实跑一次确认，不能只看文件在不在
        $ver = Invoke-Native -Exe $PY_EXE -Arguments @("--version")
        if ($ver.ExitCode -ne 0 -or $ver.Output -notmatch "3\.12") {
            Write-Log "Python 版本校验失败: 退出码=$($ver.ExitCode) 输出=$($ver.Output)" "ERROR"
            Stop-WithError -Title "运行环境无法启动" `
                -Reason "程序文件已就位，但运行起来不正常。常见原因是杀毒软件拦截，或者系统缺少微软的运行库。" `
                -Solutions @(
                    "暂时关闭杀毒软件后重新双击 安装.bat",
                    "安装微软官方运行库后重试，下载地址：https://aka.ms/vs/17/release/vc_redist.x64.exe",
                    "重启电脑后再试一次",
                    "仍然失败请把 deploy\install.log 发给作者"
                )
        }

        Say-Ok "运行环境安装完成（$($ver.Output.Trim())）"

        # 安装包用完就删，省 44MB。真要重装时重新下也不慢。
        try { Remove-Item -Path $PY_PKG -Force } catch { }

        Set-StepDone "python"
    }


    # ==========================================================
    # 第 4 步：安装依赖组件
    # ==========================================================
    Say-Step "第 4 步 / 共 $TOTAL_STEPS 步：安装程序组件"

    # 产物校验：能不能 import 才是"装好了"的唯一标准。
    # 装包成功不等于能用——faiss 依赖的运行库缺失时，只有真 import 才会暴露。
    $depsCheckCode = "import faiss, fastapi, uvicorn, jieba, openai; print('DEPS_OK')"
    $depsReady = $false
    if (Test-StepDone "deps") {
        $chk = Invoke-Native -Exe $PY_EXE -Arguments @("-c", $depsCheckCode)
        if ($chk.ExitCode -eq 0 -and $chk.Output -match "DEPS_OK") {
            $depsReady = $true
        } else {
            Write-Log "已装依赖校验失败: $($chk.Output)" "WARN"
        }
    }

    if ($depsReady) {
        Say-Skip "程序组件已安装，跳过"
    } else {
        if (Test-StepDone "deps") {
            Clear-StepDone "deps"
            Say-Warn "上次装好的组件不完整，将重新安装"
        }

        if (-not (Test-Path $PYPROJECT)) {
            Stop-WithError -Title "项目文件不完整" `
                -Reason "找不到组件清单文件 rag\pyproject.toml，无法知道需要装哪些东西。这说明项目文件没有下载完整。" `
                -Solutions @(
                    "重新完整下载本项目的压缩包并解压，不要只复制部分文件",
                    "如果是用 Git 克隆的，在项目文件夹里执行 git status 看看有没有文件缺失",
                    "联系作者重新获取完整的项目文件"
                )
        }

        $deps = @(Get-PyProjectDeps -Path $PYPROJECT)
        if ($deps.Count -eq 0) {
            Stop-WithError -Title "读不出组件清单" `
                -Reason "组件清单文件 rag\pyproject.toml 存在，但里面没有读到任何组件名，文件可能已损坏。" `
                -Solutions @(
                    "重新下载本项目的完整文件后再安装",
                    "把 rag\pyproject.toml 这个文件发给作者确认"
                )
        }
        Say-Info "需要安装 $($deps.Count) 个组件：$($deps -join ', ')"

        # ensurepip 是保险措施：这个 Python 发行版自带 pip，
        # 但万一被裁剪过，这一步能把它补回来。失败也不要紧，继续往下试。
        $ep = Invoke-Native -Exe $PY_EXE -Arguments @("-m", "ensurepip", "--default-pip")
        if ($ep.ExitCode -ne 0) {
            Write-Log "ensurepip 返回非 0（通常无害，因为 pip 本来就在）: $($ep.Output)" "WARN"
        }

        Write-Host ""
        Say "正在安装程序组件，这一步大约需要 3-10 分钟。"
        Say-Info "下面滚动的灰色文字是安装过程的记录，属于正常现象。"
        Say-Info "请不要关闭窗口，耐心等待它跑完。"
        Write-Host ""

        $pipArgs = @("-m", "pip", "install", "--no-warn-script-location") + $deps
        $pipCode = Invoke-NativeStreaming -Exe $PY_EXE -Arguments $pipArgs

        if ($pipCode -ne 0) {
            # 官方源在国内经常慢到超时。换清华镜像重试一次，
            # 这一招能解决绝大部分国内用户的安装失败。
            Write-Host ""
            Say-Warn "从默认软件源安装失败，正在改用国内镜像源重试..."
            Say-Info "镜像源在国内速度更快，这次通常会成功。"
            Write-Host ""

            $pipArgs2 = @(
                "-m", "pip", "install", "--no-warn-script-location",
                "--index-url", $PIP_MIRROR
            ) + $deps
            $pipCode = Invoke-NativeStreaming -Exe $PY_EXE -Arguments $pipArgs2
        }

        Write-Host ""
        if ($pipCode -ne 0) {
            Stop-WithError -Title "程序组件安装失败" `
                -Reason "从两个软件源下载组件都失败了。最常见的原因是网络不稳定，其次是杀毒软件拦截了安装过程。" `
                -Solutions @(
                    "检查网络是否稳定，然后重新双击 安装.bat 再试一次（已装好的部分不会重装）",
                    "暂时关闭杀毒软件或把项目文件夹加入白名单后重试",
                    "如果你在公司或学校网络下，试试换成手机热点",
                    "仍然失败请把 deploy\install.log 发给作者，里面有完整的出错记录"
                )
        }

        # 装完实测一次导入，这才算真的成功
        Say "正在检查组件是否可用..."
        $chk = Invoke-Native -Exe $PY_EXE -Arguments @("-c", $depsCheckCode)
        if ($chk.ExitCode -ne 0 -or $chk.Output -notmatch "DEPS_OK") {
            Write-Log "依赖导入校验失败: $($chk.Output)" "ERROR"
            Stop-WithError -Title "组件装上了但用不了" `
                -Reason "组件下载安装过程没报错，但实际使用时加载不起来。这通常是系统缺少微软的运行库，或者部分文件被杀毒软件删掉了。" `
                -Solutions @(
                    "安装微软官方运行库后重试，下载地址：https://aka.ms/vs/17/release/vc_redist.x64.exe（下载后双击安装，然后重启电脑）",
                    "检查杀毒软件的隔离区，恢复被误删的文件并把项目文件夹加入白名单",
                    "删除项目文件夹里的 python-embed 文件夹，然后重新双击 安装.bat 彻底重装一遍运行环境",
                    "仍然失败请把 deploy\install.log 发给作者"
                )
        }

        Say-Ok "程序组件安装完成（共 $($deps.Count) 个）"
        Set-StepDone "deps"
    }


    # ==========================================================
    # 第 5 步：填写配置文件
    # ==========================================================
    Say-Step "第 5 步 / 共 $TOTAL_STEPS 步：填写 API 密钥"

    # 这一步的产物是 rag\.env。它里面是用户的私人密钥，
    # 绝不能覆盖：用户可能早就手工填好了，覆盖等于把他的密钥抹掉。
    $envReady = $false
    if (Test-Path $ENV_FILE) {
        $k1 = Get-EnvValue -Path $ENV_FILE -Key "OPENAI_API_KEY"
        $k2 = Get-EnvValue -Path $ENV_FILE -Key "EMBED_API_KEY"
        if ((Test-KeyFilled $k1) -and (Test-KeyFilled $k2)) { $envReady = $true }
    }

    if ($envReady) {
        Say-Skip "API 密钥已填写，跳过（不会覆盖你已经填好的内容）"
        Set-StepDone "envfile"
    } else {
        if (Test-StepDone "envfile") {
            Clear-StepDone "envfile"
            Say-Warn "配置文件丢失或密钥被清空了，需要重新填写"
        }

        if (-not (Test-Path $ENV_FILE)) {
            if (-not (Test-Path $ENV_SAMPLE)) {
                Stop-WithError -Title "项目文件不完整" `
                    -Reason "找不到配置模板 rag\.env.example，无法生成配置文件。" `
                    -Solutions @(
                        "重新下载本项目的完整文件后再安装",
                        "联系作者索取 .env.example 这个文件"
                    )
            }
            Copy-Item -Path $ENV_SAMPLE -Destination $ENV_FILE -Force
            Say-Ok "已生成配置文件 rag\.env"
        } else {
            Say-Info "配置文件已存在，将在它的基础上继续填写"
        }

        # 把要填什么讲清楚再打开记事本。
        # 用户面对一屏英文配置项时最容易懵，这段说明是整个安装里最关键的引导。
        Write-Host ""
        Write-Host "  ---------------------------------------------------------" -ForegroundColor Yellow
        Write-Host "   接下来需要你亲自填两个密钥（这是唯一需要手动做的事）" -ForegroundColor Yellow
        Write-Host "  ---------------------------------------------------------" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  按回车后会自动弹出「记事本」，里面有一份配置清单。"
        Write-Host "  你需要找到下面这两行，把等号后面的内容换成你自己的密钥："
        Write-Host ""
        Write-Host "  【1】OPENAI_API_KEY=sk-your-api-key" -ForegroundColor White
        Write-Host "       这是「回答问题」用的大模型密钥。"
        Write-Host "       申请地址：https://platform.deepseek.com/" -ForegroundColor Cyan
        Write-Host "       注册登录后进入「API keys」页面，点「创建 API key」，"
        Write-Host "       把生成的那串 sk- 开头的字符复制过来。"
        Write-Host "       注意：密钥只在创建时显示一次，记得先复制下来。"
        Write-Host ""
        Write-Host "  【2】EMBED_API_KEY=sk-your-embedding-api-key" -ForegroundColor White
        Write-Host "       这是「查找资料」用的向量检索密钥。"
        Write-Host "       申请地址：https://siliconflow.cn/" -ForegroundColor Cyan
        Write-Host "       注册登录后在「API 密钥」页面新建一个，同样是 sk- 开头。"
        Write-Host "       其他兼容 OpenAI 格式的服务也可以用，"
        Write-Host "       但要连同上面一行的 EMBED_API_BASE_URL 一起改成对应地址。"
        Write-Host ""
        Write-Host "  填写要点：" -ForegroundColor Yellow
        Write-Host "    - 只改等号后面的部分，等号前面的名字一个字都不要动"
        Write-Host "    - 不要加引号，不要在前后留空格"
        Write-Host "    - 以 # 开头的行是说明文字，不用管它"
        Write-Host "    - 改完按 Ctrl+S 保存，然后直接关掉记事本窗口"
        Write-Host ""
        Write-Host "  两个密钥都是免费注册的，新用户一般都送额度，足够试用。" -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  准备好了就按回车键，记事本会自动打开..." -ForegroundColor Yellow
        try { Read-Host | Out-Null } catch { }

        # -Wait 会一直卡在这里，直到用户关掉记事本，
        # 这正是我们要的：不关记事本就说明还没填完，不能往下走
        Say "记事本已打开，填好并保存后，请关闭记事本窗口，安装会自动继续..."
        try {
            Start-Process notepad.exe -ArgumentList $ENV_FILE -Wait
        } catch {
            Write-Log "打开记事本失败: $($_.Exception.Message)" "WARN"
            Say-Warn "记事本没能自动打开，请手动用记事本打开这个文件填写：$ENV_FILE"
            Write-Host "  填好保存后，回到这里按回车继续..." -ForegroundColor Yellow
            try { Read-Host | Out-Null } catch { }
        }

        # 重新读一遍，确认用户真的填了。
        # 只看是不是还是占位符，绝不联网验证密钥真假——
        # 那会消耗用户额度，而且服务商临时抽风会造成误判。
        $k1 = Get-EnvValue -Path $ENV_FILE -Key "OPENAI_API_KEY"
        $k2 = Get-EnvValue -Path $ENV_FILE -Key "EMBED_API_KEY"

        $missing = @()
        if (-not (Test-KeyFilled $k1)) { $missing += "OPENAI_API_KEY（回答问题用的大模型密钥）" }
        if (-not (Test-KeyFilled $k2)) { $missing += "EMBED_API_KEY（查找资料用的检索密钥）" }

        if ($missing.Count -gt 0) {
            Stop-WithError -Title "密钥还没有填写" `
                -Reason "配置文件里下面这些还是原样没改：$($missing -join '、')。没有真实密钥，程序装好了也没法回答问题。" `
                -Solutions @(
                    "重新双击 安装.bat，会再次弹出记事本让你填（前面装好的部分不会重装，很快就走到这一步）",
                    "也可以直接用记事本打开 $ENV_FILE 自己改，改完保存再运行 安装.bat",
                    "大模型密钥在 https://platform.deepseek.com/ 免费注册申请",
                    "检索密钥在 https://siliconflow.cn/ 免费注册申请",
                    "注意只替换等号后面的部分，比如把 OPENAI_API_KEY=sk-your-api-key 改成 OPENAI_API_KEY=sk-你申请到的那串字符"
                )
        }

        # 检索服务地址还是模板里的示例值时提醒一句。
        # 不拦截：用户可能用的是自建服务，地址本来就该由他自己定。
        $embedUrl = Get-EnvValue -Path $ENV_FILE -Key "EMBED_API_BASE_URL"
        if ($embedUrl -like "*your-api-endpoint.example.com*") {
            Say-Warn "EMBED_API_BASE_URL 还是示例地址，可能连不上检索服务"
            Say-Info "如果你用的是硅基流动，请把这一行改成：EMBED_API_BASE_URL=https://api.siliconflow.cn/v1"
            Say-Info "现在可以先继续装，启动后发现搜不到资料时再回来改这一行也来得及。"
        }

        Say-Ok "API 密钥已填写"
        Set-StepDone "envfile"
    }


    # ==========================================================
    # 第 6 步：下载知识库
    # ==========================================================
    Say-Step "第 6 步 / 共 $TOTAL_STEPS 步：下载知识库（约 1.2 GB）"

    # 逐个文件校验，只下缺的那个。
    # 这是整个安装里最耗时的一步，多下一个字节都是在浪费用户的时间和流量。
    $needDownload = @()
    foreach ($item in $INDEX_FILES) {
        $dest = Join-Path $RAG_DIR $item.File
        if (Test-FileValid -Path $dest -MinSize $MIN_INDEX_SIZE) {
            Say-Skip "$($item.Name) 已存在（$(Format-Size (Get-Item $dest).Length)）"
        } else {
            if (Test-Path $dest) {
                # 文件在但不合格：多半是 Git LFS 的指针文件或残缺下载。
                # 留着它只会让后面的校验反复失败，直接删掉重下更省事。
                Say-Warn "$($item.Name) 不完整，将重新下载"
                try { Remove-Item -Path $dest -Force } catch { }
            }
            $needDownload += $item
        }
    }

    if ($needDownload.Count -eq 0) {
        Say-Ok "知识库文件齐全，无需下载"
        Set-StepDone "index"
    } else {
        if (Test-StepDone "index") {
            Clear-StepDone "index"
            Say-Warn "有知识库文件丢失了，需要重新下载"
        }

        Write-Host ""
        Say "需要下载 $($needDownload.Count) 个文件，合计约 1.2 GB。"
        Say-Info "视网速需要 10-60 分钟。下载支持断点续传：中途关掉窗口也没关系，"
        Say-Info "下次重新运行 安装.bat 会从断掉的地方接着下，不会重头来。"
        Write-Host ""

        # 先探一下路。知识库放在 GitHub Releases 上，如果作者还没发布、
        # 或者仓库是私有的，服务器会回 404。这种情况重试一万次也没用，
        # 必须一眼识破并告诉用户"这不是你的问题，去找作者要"。
        $probeUrl = "$INDEX_BASE/$($needDownload[0].File)"
        $status = Get-UrlStatusCode -Url $probeUrl
        Write-Log "知识库探测 $probeUrl -> HTTP $status" "INFO"

        if ($status -eq 404 -or $status -eq 403 -or $status -eq 401) {
            Stop-WithError -Title "知识库暂时无法下载" `
                -Reason "知识库数据尚未发布，或者当前无法公开访问（服务器返回 $status）。这不是你的电脑或网络的问题。" `
                -Solutions @(
                    "请联系项目作者获取知识库文件（documents.db、faiss_index.idx、bm25_index.pkl 三个文件）",
                    "拿到文件后，把它们直接放进项目的 rag 文件夹里，再重新双击 安装.bat 即可继续",
                    "前面已经装好的运行环境和组件都会保留，不会重装",
                    "如果作者稍后发布了知识库，直接重新运行 安装.bat 就能自动下载"
                )
        }

        if ($status -eq 0) {
            Say-Warn "暂时连不上知识库服务器，仍然尝试下载一次"
            Say-Info "如果你有网络加速器或代理软件，建议现在打开它。"
        }

        $idx = 0
        foreach ($item in $needDownload) {
            $idx++
            $dest = Join-Path $RAG_DIR $item.File
            $url = "$INDEX_BASE/$($item.File)"

            Write-Host ""
            Say "[$idx/$($needDownload.Count)] 正在下载 $($item.Name)（约 $(Format-Size $item.Size)）"

            $ok = Invoke-Download -Url $url -OutFile $dest -Description $item.Name

            if (-not $ok) {
                # 下载失败后再探一次状态码：是"文件不存在"还是"网不好"，
                # 这两种情况给用户的建议完全不同，不能混为一谈。
                $st = Get-UrlStatusCode -Url $url
                Write-Log "下载失败后复查 $url -> HTTP $st" "INFO"

                if ($st -eq 404 -or $st -eq 403 -or $st -eq 401) {
                    Stop-WithError -Title "知识库暂时无法下载" `
                        -Reason "服务器上找不到 $($item.Name) 这个文件（返回 $st），说明知识库数据尚未发布或无法公开访问。这不是你的问题。" `
                        -Solutions @(
                            "请联系项目作者获取知识库文件",
                            "拿到 $($item.File) 后直接放进项目的 rag 文件夹，再重新运行 安装.bat",
                            "已经装好的部分会保留，不会重装"
                        )
                }

                Stop-WithError -Title "知识库下载中断" `
                    -Reason "下载 $($item.Name) 时网络反复中断，重试多次仍未成功。" `
                    -Solutions @(
                        "重新双击 安装.bat，会从断掉的地方接着下，已下载的部分不会浪费",
                        "如果你有网络加速器或代理软件，打开它之后再重试",
                        "换个网络环境试试，或者选在网络空闲的时段（比如深夜）下载",
                        "文件较大，建议用有线网络或稳定的 WiFi，不要用流量"
                    )
            }

            # 下完立刻验一遍大小，挡住"下了个错误页面存成文件"这种情况
            if (-not (Test-FileValid -Path $dest -MinSize $MIN_INDEX_SIZE)) {
                $actual = 0
                if (Test-Path $dest) { $actual = (Get-Item $dest).Length }
                try { Remove-Item -Path $dest -Force } catch { }
                Stop-WithError -Title "下载到的文件不对" `
                    -Reason "$($item.Name) 下载完成了，但文件只有 $(Format-Size $actual)，远小于它应有的大小。下到的很可能是一个错误提示页面而不是真正的数据。" `
                    -Solutions @(
                        "有问题的文件已经自动删除，重新双击 安装.bat 会重新下载它",
                        "如果反复出现这个问题，可能是网络运营商或代理软件篡改了下载内容，试试换个网络",
                        "仍然失败请联系作者，直接索取知识库文件"
                    )
            }

            Say-Ok "$($item.Name) 下载完成"
        }

        Say-Ok "知识库全部就绪"
        Set-StepDone "index"
    }


    # ==========================================================
    # 第 7 步：下载原文语料（可选）
    # ==========================================================
    Say-Step "第 7 步 / 共 $TOTAL_STEPS 步：下载原文资料（可选，约 152 MB）"

    # 这一步是可选功能，从头到尾都不能中断安装。
    # 用户不下、下失败、解压失败，都只是"少个阅读器"，问答照样能用。
    $wwCount = 0
    if (Test-Path $WW_DIR) {
        $mdFiles = @(Get-ChildItem -Path $WW_DIR -Filter "*.md" -File)
        # README.md 是项目自带的说明文件，不算语料
        $wwCount = @($mdFiles | Where-Object { $_.Name -ne "README.md" }).Count
    }

    if ($wwCount -gt 0 -and (Test-StepDone "corpus")) {
        Say-Skip "原文资料已存在（$wwCount 篇），跳过"
    } elseif ($wwCount -gt 0) {
        # 有文件但没标记：用户可能是自己拷进去的，认它，别多此一举再下一遍
        Say-Skip "检测到已有原文资料（$wwCount 篇），跳过下载"
        Set-StepDone "corpus"
    } else {
        if (Test-StepDone "corpus") {
            Clear-StepDone "corpus"
            Say-Warn "原文资料不见了，可以重新下载"
        }

        Write-Host ""
        Write-Host "  原文资料是什么：马列经典著作的完整原文（约 130 篇）。" -ForegroundColor White
        Write-Host ""
        Write-Host "  下载后：回答问题时可以点击引用，直接跳到原文对照阅读。"
        Write-Host "  不下载：问答功能完全正常，只是点引用时看不到原文全文。"
        Write-Host ""
        Write-Host "  大小约 152 MB，视网速需要 2-10 分钟。"
        Write-Host "  以后想要也可以随时重新运行 安装.bat 来补下。" -ForegroundColor DarkGray
        Write-Host ""

        $answer = "Y"
        try {
            # 变量名不能叫 $input：那是 PowerShell 的保留变量（管道输入），
            # 覆盖它会让后面的管道行为变得难以预料
            $reply = Read-Host "  现在下载原文资料吗？直接按回车表示下载 [Y/n]"
            if ("$reply".Trim()) { $answer = "$reply".Trim() }
        } catch {
            # 读不到输入（比如被自动化调用）就按默认走，不卡住流程
        }

        if ($answer -match "^[Nn]") {
            Say-Skip "已跳过原文资料下载"
            Say-Info "以后想下载的话，重新双击 安装.bat 会再问一次。"
        } else {
            $corpusOk = $false
            $tmpDir = Join-Path $script:CACHE_DIR "corpus_extract"

            try {
                if (-not (Test-FileValid -Path $CORPUS_ZIP -MinSize 10MB)) {
                    Say "正在下载原文资料（约 152 MB）..."
                    $corpusOk = Invoke-Download -Url $CORPUS_URL -OutFile $CORPUS_ZIP -Description "原文资料"
                } else {
                    Say-Skip "原文压缩包已下载过，直接使用"
                    $corpusOk = $true
                }

                if ($corpusOk) {
                    Say "正在解压原文资料，请稍候..."

                    # 上次解压的残留会让文件数对不上，先清干净
                    if (Test-Path $tmpDir) {
                        Remove-Item -Path $tmpDir -Recurse -Force
                    }
                    New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

                    Expand-Archive -Path $CORPUS_ZIP -DestinationPath $tmpDir -Force

                    # GitHub 下载的源码包会多套一层「仓库名-分支名」目录，
                    # 真正的 md 文件在它里面。找不到就退而求其次，直接全盘搜。
                    $srcDir = Join-Path $tmpDir $CORPUS_SUB
                    if (-not (Test-Path $srcDir)) { $srcDir = $tmpDir }

                    if (-not (Test-Path $WW_DIR)) {
                        New-Item -ItemType Directory -Path $WW_DIR -Force | Out-Null
                    }

                    $moved = 0
                    $skipped = 0
                    foreach ($f in @(Get-ChildItem -Path $srcDir -Filter "*.md" -File -Recurse)) {
                        # 压缩包根目录里的 README.md 是那个仓库自己的说明文件，
                        # 而 ww\README.md 是本项目的说明。同名，一拷就把我们的覆盖了。
                        # 语料本身不会叫这个名字，所以整个跳过最安全。
                        if ($f.Name -eq "README.md") {
                            $skipped++
                            continue
                        }
                        Copy-Item -Path $f.FullName -Destination (Join-Path $WW_DIR $f.Name) -Force
                        $moved++
                    }

                    if ($moved -gt 0) {
                        Say-Ok "原文资料已就位（$moved 篇）"
                        if ($skipped -gt 0) {
                            Say-Info "已自动跳过 $skipped 个说明文件，避免覆盖项目自带的 ww\README.md"
                        }
                        Set-StepDone "corpus"
                    } else {
                        Say-Warn "压缩包里没有找到原文文件，跳过这一步"
                        Say-Info "问答功能不受影响，只是点击引用时看不到原文。"
                    }
                } else {
                    Say-Warn "原文资料下载失败，已跳过这一步"
                    Say-Info "问答功能不受影响。想再试的话，重新双击 安装.bat 即可。"
                }
            } catch {
                # 可选功能的任何意外都不许影响主流程
                Write-Log "语料处理失败: $($_.Exception.Message)" "WARN"
                Say-Warn "原文资料处理时出了点问题，已跳过这一步"
                Say-Info "问答功能不受影响。想再试的话，重新双击 安装.bat 即可。"
            } finally {
                # 解压出来的临时文件和 152MB 的压缩包都清掉，不占用户磁盘
                try {
                    if (Test-Path $tmpDir) { Remove-Item -Path $tmpDir -Recurse -Force }
                } catch { }
                try {
                    if ((Test-StepDone "corpus") -and (Test-Path $CORPUS_ZIP)) {
                        Remove-Item -Path $CORPUS_ZIP -Force
                    }
                } catch { }
            }
        }
    }


    # ==========================================================
    # 完成
    # ==========================================================
    Write-Host ""
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host "  安装全部完成" -ForegroundColor Green
    Write-Host "===============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  现在可以开始使用了：" -ForegroundColor White
    Write-Host ""
    Write-Host "    1. 关掉这个窗口"
    Write-Host "    2. 回到项目文件夹，双击「启动.bat」" -ForegroundColor Yellow
    Write-Host "    3. 稍等 30 秒到 2 分钟，浏览器会自动打开"
    Write-Host ""
    Write-Host "  访问地址：http://localhost:8000" -ForegroundColor Cyan
    Write-Host "  （浏览器如果没自动打开，把这个地址复制到浏览器里即可）"
    Write-Host ""
    Write-Host "  用完想关闭服务：双击「关闭.bat」" -ForegroundColor White
    Write-Host ""
    Write-Host "  首次启动会把知识库读进内存，比较慢，属于正常现象。" -ForegroundColor DarkGray
    Write-Host "  安装记录保存在：$script:LOG_FILE" -ForegroundColor DarkGray
    Write-Host ""

    Write-Log "===== 安装全部完成 =====" "INFO"
    exit 0

} catch {
    # 兜底：任何没预料到的异常都在这里转成人话，绝不把堆栈甩给用户
    Write-Log "未预期的异常: $($_.Exception.Message)" "ERROR"
    try { Write-Log "异常位置: $($_.InvocationInfo.PositionMessage)" "ERROR" } catch { }
    Stop-WithError -Title "安装过程中出现意外问题" `
        -Reason "安装程序遇到了没有预料到的情况，已经停下来了。已完成的步骤都已保存，不会白做。" `
        -Solutions @(
            "重新双击 安装.bat 再试一次，它会从断掉的地方继续，通常能自行恢复",
            "如果反复失败，试试暂时关闭杀毒软件后再运行",
            "确认项目文件夹没有被移动位置，磁盘空间也还充足",
            "仍然失败请把 deploy\install.log 发给作者，里面记录了详细信息"
        )
}

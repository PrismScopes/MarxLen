# ============================================================
# 网络与下载
#
# 单独成文件，因为这部分逻辑最重：1.2GB 的下载不能失败一次就从头来，
# 必须支持断点续传、完整性校验与失败重试。
# ============================================================

function Test-Network {
    <#
      检测能否访问某个站点。

      用 HEAD 而不是 GET，避免为了探活白下几 MB。
      超时设短一点（10 秒），断网时不让用户干等。
    #>
    param(
        [string]$Url,
        [int]$TimeoutSec = 10
    )
    try {
        $null = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing `
            -TimeoutSec $TimeoutSec -Headers @{ "User-Agent" = "MarxLen-Installer" }
        return $true
    } catch {
        # 4xx 也算通：说明网络本身是通的，只是路径不对
        try {
            $code = [int]$_.Exception.Response.StatusCode
            if ($code -ge 400 -and $code -lt 500) { return $true }
        } catch { }
        return $false
    }
}

function Get-RemoteSize {
    <# 取远端文件字节数。取不到返回 0（有些服务器不给 Content-Length）#>
    param([string]$Url)
    try {
        $r = Invoke-WebRequest -Uri $Url -Method Head -UseBasicParsing -TimeoutSec 20 `
            -Headers @{ "User-Agent" = "MarxLen-Installer" }
        $len = $r.Headers['Content-Length']
        # PS5.1 下 Headers 取值可能是数组，取首个元素
        if ($len -is [array]) { $len = $len[0] }
        if ($len) { return [int64]$len }
        return 0
    } catch {
        return 0
    }
}

function Format-Size {
    param([int64]$Bytes)
    if ($Bytes -ge 1GB) { return "{0:N2} GB" -f ($Bytes / 1GB) }
    if ($Bytes -ge 1MB) { return "{0:N1} MB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N0} KB" -f ($Bytes / 1KB) }
    return "$Bytes 字节"
}

function Invoke-Download {
    <#
      带断点续传与进度显示的下载。

      为什么不用 Invoke-WebRequest 直接下：
        它会把整个响应读进内存，1.2GB 的文件会直接把内存吃爆，
        而且中断后无法续传。这里改用 HttpWebRequest + 流式写盘，
        配合 Range 头实现续传。

      落盘策略：先写 .part 临时文件，校验通过后才改名为正式文件。
      这样中途断电也不会留下一个看似完整、实则残缺的文件。
    #>
    param(
        [string]$Url,
        [string]$OutFile,
        [string]$Description = "文件",
        [int64]$ExpectedSize = 0,
        [int]$MaxRetry = 3
    )

    $partFile = "$OutFile.part"
    $dir = Split-Path -Parent $OutFile
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    # 远端大小：优先用调用方给的期望值，避免多一次请求
    $totalSize = $ExpectedSize
    if ($totalSize -le 0) {
        $totalSize = Get-RemoteSize -Url $Url
    }

    for ($attempt = 1; $attempt -le $MaxRetry; $attempt++) {

        # 已下载的字节数，决定从哪里续传
        $existing = 0
        if (Test-Path $partFile) {
            $existing = (Get-Item $partFile).Length
            # 已下够了，说明上次只差改名这一步
            if ($totalSize -gt 0 -and $existing -ge $totalSize) {
                Move-Item -Path $partFile -Destination $OutFile -Force
                return $true
            }
            if ($existing -gt 0) {
                Say-Info "发现未下完的文件，从 $(Format-Size $existing) 处继续"
            }
        }

        $fileStream = $null
        $respStream = $null
        $response = $null

        try {
            $req = [System.Net.HttpWebRequest]::Create($Url)
            $req.UserAgent = "MarxLen-Installer"
            $req.Timeout = 60000            # 建连超时 60 秒
            $req.ReadWriteTimeout = 300000  # 读写超时 5 分钟，大文件慢速时不误杀
            if ($existing -gt 0) {
                $req.AddRange($existing)
            }

            $response = $req.GetResponse()
            $respStream = $response.GetResponseStream()

            # 服务器是否接受了续传请求
            $isResume = $false
            try {
                if ([int]$response.StatusCode -eq 206) { $isResume = $true }
            } catch { }

            if ($existing -gt 0 -and -not $isResume) {
                # 服务器不支持续传，只能从头下
                Say-Info "服务器不支持断点续传，重新下载"
                $existing = 0
                $mode = [System.IO.FileMode]::Create
            } elseif ($existing -gt 0) {
                $mode = [System.IO.FileMode]::Append
            } else {
                $mode = [System.IO.FileMode]::Create
            }

            if ($totalSize -le 0) {
                $totalSize = $existing + $response.ContentLength
            }

            $fileStream = New-Object System.IO.FileStream($partFile, $mode, [System.IO.FileAccess]::Write)

            $buffer = New-Object byte[] 1048576   # 1MB 缓冲，兼顾速度与内存
            $downloaded = $existing
            $lastReport = [DateTime]::Now
            $startTime = [DateTime]::Now

            while ($true) {
                $read = $respStream.Read($buffer, 0, $buffer.Length)
                if ($read -le 0) { break }
                $fileStream.Write($buffer, 0, $read)
                $downloaded += $read

                # 每秒刷新一次进度，刷太频繁反而拖慢下载
                if (([DateTime]::Now - $lastReport).TotalSeconds -ge 1) {
                    $elapsed = ([DateTime]::Now - $startTime).TotalSeconds
                    $speed = 0
                    if ($elapsed -gt 0) { $speed = ($downloaded - $existing) / $elapsed }
                    if ($totalSize -gt 0) {
                        $pct = [math]::Min(100, [math]::Round($downloaded * 100.0 / $totalSize, 1))
                        $etaText = ""
                        if ($speed -gt 0) {
                            $remain = ($totalSize - $downloaded) / $speed
                            if ($remain -gt 60) {
                                $etaText = "，剩余约 $([math]::Ceiling($remain / 60)) 分钟"
                            } else {
                                $etaText = "，剩余约 $([math]::Ceiling($remain)) 秒"
                            }
                        }
                        Write-Host ("`r         {0}  {1}%  {2}/{3}  {4}/秒{5}        " -f `
                            $Description, $pct, (Format-Size $downloaded), (Format-Size $totalSize), `
                            (Format-Size ([int64]$speed)), $etaText) -NoNewline
                    } else {
                        Write-Host ("`r         {0}  已下载 {1}  {2}/秒        " -f `
                            $Description, (Format-Size $downloaded), (Format-Size ([int64]$speed))) -NoNewline
                    }
                    $lastReport = [DateTime]::Now
                }
            }

            Write-Host ""   # 结束进度行

            $fileStream.Close(); $fileStream = $null
            $respStream.Close(); $respStream = $null
            $response.Close(); $response = $null

            # 大小校验：少一个字节都算失败，避免半截文件被当成好文件
            $actual = (Get-Item $partFile).Length
            if ($totalSize -gt 0 -and $actual -ne $totalSize) {
                throw "文件大小不符，期望 $totalSize 字节，实际 $actual 字节"
            }

            Move-Item -Path $partFile -Destination $OutFile -Force
            Write-Log "下载成功: $Url -> $OutFile ($actual 字节)" "INFO"
            return $true

        } catch {
            $errMsg = $_.Exception.Message
            Write-Log "下载失败(第 $attempt 次): $Url | $errMsg" "WARN"

            if ($fileStream) { try { $fileStream.Close() } catch { } }
            if ($respStream) { try { $respStream.Close() } catch { } }
            if ($response) { try { $response.Close() } catch { } }

            if ($attempt -lt $MaxRetry) {
                Write-Host ""
                Say-Warn "下载中断（$errMsg），5 秒后重试（第 $attempt/$MaxRetry 次）"
                Start-Sleep -Seconds 5
            } else {
                Write-Host ""
                Write-Log "下载最终失败: $Url" "ERROR"
                return $false
            }
        }
    }
    return $false
}

function Test-FileValid {
    <#
      校验文件是否可用。

      只比大小、不算哈希：1.2GB 算 SHA256 要花几十秒，
      而大小校验已能挡住绝大多数残缺文件（下载中断、磁盘满）。
      $MinSize 用于挡住"下了个 404 页面存成文件"这种情况。
    #>
    param(
        [string]$Path,
        [int64]$ExpectedSize = 0,
        [int64]$MinSize = 0
    )
    if (-not (Test-Path $Path)) { return $false }
    $len = (Get-Item $Path).Length
    if ($ExpectedSize -gt 0) { return ($len -eq $ExpectedSize) }
    if ($MinSize -gt 0) { return ($len -ge $MinSize) }
    return ($len -gt 0)
}

<#
.SYNOPSIS
    EchoMind 接口冒烟测试（Windows，PowerShell 5.1 / 7 通用）。

.DESCRIPTION
    依次请求 /health、/skills、/knowledge/stats、/monitor、/metrics，
    可选请求 /chat；每项输出 HTTP 状态码、耗时和关键字段摘要，
    结尾给出汇总表与退出码。不依赖 curl，Windows 自带 PowerShell 即可运行。

    注意：/chat 需要真实可用的 ANTHROPIC_API_KEY；key 仍是占位值时请加 -SkipChat。

.PARAMETER BaseUrl
    服务地址，默认 http://localhost:8000（直连主应用）。
    若要通过 Nginx 反代（80 端口）访问，用 -BaseUrl http://localhost 。

.PARAMETER SkipChat
    跳过 /chat。没有配置真实 API Key 时建议跳过，否则该接口必然失败。

.PARAMETER TimeoutSec
    普通接口的超时时间（秒），默认 10。

.PARAMETER ChatTimeoutSec
    /chat 的超时时间（秒），默认 120（内部会串行调用大模型，比较慢）。

.PARAMETER Help
    显示帮助信息后退出。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipChat

.EXAMPLE
    pwsh -NoProfile -File .\scripts\smoke.ps1 -BaseUrl http://localhost

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipChat -BaseUrl http://localhost:8000

.NOTES
    退出码：0 = 全部通过（SKIP 不计失败）；1 = 有接口失败；
            2 = 服务整体不可达（连接不上）；3 = 参数或脚本自身异常（已捕获，不抛裸堆栈）。
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://localhost:8000',
    [switch]$SkipChat,
    [ValidateRange(1, 600)]
    [int]$TimeoutSec = 10,
    [ValidateRange(1, 600)]
    [int]$ChatTimeoutSec = 120,
    [switch]$Help
)

$ErrorActionPreference = 'Continue'

# ══════════════════════════════════════════════════════════════════════════════
# 帮助
# ══════════════════════════════════════════════════════════════════════════════
function Show-HelpText {
    Write-Host ''
    Write-Host 'EchoMind 接口冒烟测试 (smoke.ps1)' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '用法:' -ForegroundColor White
    Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 [-BaseUrl <url>] [-SkipChat]'
    Write-Host '  pwsh -NoProfile -File .\scripts\smoke.ps1 -SkipChat'
    Write-Host ''
    Write-Host '参数:' -ForegroundColor White
    Write-Host '  -BaseUrl <url>       服务地址，默认 http://localhost:8000'
    Write-Host '                       走 Nginx(80) 用 http://localhost'
    Write-Host '  -SkipChat            跳过 /chat（需要真实 API Key）'
    Write-Host '  -TimeoutSec <n>      普通接口超时秒数，默认 10'
    Write-Host '  -ChatTimeoutSec <n>  /chat 超时秒数，默认 120'
    Write-Host '  -Help                显示本帮助后退出'
    Write-Host ''
    Write-Host '退出码: 0=全部通过, 1=有接口失败, 2=服务不可达, 3=脚本异常' -ForegroundColor DarkGray
    Write-Host ''
}

if ($Help) {
    Show-HelpText
    exit 0
}

# ══════════════════════════════════════════════════════════════════════════════
# 输出辅助
# ══════════════════════════════════════════════════════════════════════════════
function Get-TextWidth {
    param([string]$Text)
    if ($null -eq $Text) { return 0 }
    $width = 0
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        $isWide = $false
        if ($code -ge 0x2E80 -and $code -le 0xA4CF) { $isWide = $true }
        elseif ($code -ge 0xAC00 -and $code -le 0xD7A3) { $isWide = $true }
        elseif ($code -ge 0xF900 -and $code -le 0xFAFF) { $isWide = $true }
        elseif ($code -ge 0xFE30 -and $code -le 0xFE6F) { $isWide = $true }
        elseif ($code -ge 0xFF00 -and $code -le 0xFF60) { $isWide = $true }
        elseif ($code -ge 0xFFE0 -and $code -le 0xFFE6) { $isWide = $true }
        if ($isWide) { $width += 2 } else { $width += 1 }
    }
    return $width
}

function Format-PadDisplay {
    param([string]$Text, [int]$Width)
    $pad = $Width - (Get-TextWidth $Text)
    if ($pad -lt 0) { $pad = 0 }
    return $Text + (' ' * $pad)
}

function Get-ShortText {
    param([string]$Text, [int]$MaxLength = 110)
    if ($null -eq $Text) { return '' }
    $one = ($Text -split "`r?`n" | Where-Object { $_ -ne '' } | Select-Object -First 1)
    if ($null -eq $one) { return '' }
    $clean = ([string]$one).Trim() -replace '\s+', ' '
    if ($clean.Length -gt $MaxLength) { $clean = $clean.Substring(0, $MaxLength) + '...' }
    return $clean
}

function Get-ColorForStatus {
    param([string]$Status)
    if ($Status -eq 'PASS') { return 'Green' }
    if ($Status -eq 'FAIL') { return 'Red' }
    if ($Status -eq 'SKIP') { return 'Yellow' }
    return 'Gray'
}

# ══════════════════════════════════════════════════════════════════════════════
# HTTP 调用（失败也要拿到状态码/响应体，绝不让异常冒泡）
# ══════════════════════════════════════════════════════════════════════════════
function Get-StatusCodeFromException {
    param([System.Exception]$Exception)
    $current = $Exception
    $guard = 0
    while ($null -ne $current -and $guard -lt 6) {
        $guard = $guard + 1
        $response = $null
        $property = $current.PSObject.Properties['Response']
        if ($null -ne $property) { $response = $property.Value }
        if ($null -ne $response) {
            $code = 0
            try { $code = [int]$response.StatusCode } catch { $code = 0 }
            if ($code -gt 0) { return $code }
            try {
                if ($null -ne $response.StatusCode.value__) { return [int]$response.StatusCode.value__ }
            } catch { }
        }
        $current = $current.InnerException
    }
    return 0
}

function Get-ErrorResponseBody {
    param([System.Exception]$Exception)
    $current = $Exception
    $guard = 0
    while ($null -ne $current -and $guard -lt 6) {
        $guard = $guard + 1
        $response = $null
        $property = $current.PSObject.Properties['Response']
        if ($null -ne $property) { $response = $property.Value }
        if ($null -ne $response) {
            # Windows PowerShell 5.1：WebException.Response 是 HttpWebResponse
            try {
                $stream = $response.GetResponseStream()
                if ($null -ne $stream) {
                    $reader = New-Object System.IO.StreamReader($stream)
                    $text = $reader.ReadToEnd()
                    $reader.Close()
                    $stream.Close()
                    if (-not [string]::IsNullOrEmpty($text)) { return $text }
                }
            } catch { }
            # PowerShell 7：HttpResponseException.Response 是 HttpResponseMessage
            try {
                $content = $response.Content
                if ($null -ne $content) {
                    $text = $content.ReadAsStringAsync().GetAwaiter().GetResult()
                    if (-not [string]::IsNullOrEmpty([string]$text)) { return [string]$text }
                }
            } catch { }
        }
        $current = $current.InnerException
    }
    return ''
}

# Windows PowerShell 5.1 对未声明 charset 的响应按 ISO-8859-1 解码，
# 导致 FastAPI 返回的中文 JSON 变成乱码；这里优先用原始字节按 UTF-8 解码。
function Get-ResponseText {
    param($Response)
    if ($null -eq $Response) { return '' }
    $contentType = ''
    try {
        $contentType = [string]$Response.Headers['Content-Type']
    } catch { $contentType = '' }

    $charsetSaysUtf8 = $true
    if ($contentType -match 'charset\s*=\s*([^;]+)') {
        $charset = $Matches[1].Trim().ToLowerInvariant()
        if ($charset -ne 'utf-8' -and $charset -ne 'utf8') { $charsetSaysUtf8 = $false }
    }

    if ($charsetSaysUtf8) {
        try {
            $stream = $Response.RawContentStream
            if ($null -ne $stream) {
                $bytes = $null
                if ($stream -is [System.IO.MemoryStream]) {
                    $bytes = $stream.ToArray()
                } else {
                    $stream.Position = 0
                    $memory = New-Object System.IO.MemoryStream
                    $stream.CopyTo($memory)
                    $bytes = $memory.ToArray()
                }
                if ($null -ne $bytes -and $bytes.Length -gt 0) {
                    return [System.Text.Encoding]::UTF8.GetString($bytes)
                }
            }
        } catch { }
    }

    try { return [string]$Response.Content } catch { return '' }
}

function Invoke-HttpCall {
    param(
        [string]$Method,
        [string]$Url,
        [int]$TimeoutSec,
        [string]$Body = ''
    )
    $call = [pscustomobject]@{
        Ok          = $false
        Code        = 0
        Content     = ''
        ErrorText   = ''
        Unreachable = $false
        ElapsedMs   = 0
    }
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $requestArgs = @{
            Uri             = $Url
            Method          = $Method
            TimeoutSec      = $TimeoutSec
            UseBasicParsing = $true
            ErrorAction     = 'Stop'
        }
        if (-not [string]::IsNullOrEmpty($Body)) {
            $requestArgs['Body'] = $Body
            $requestArgs['ContentType'] = 'application/json; charset=utf-8'
        }
        $response = Invoke-WebRequest @requestArgs
        $call.Ok = $true
        $call.Code = [int]$response.StatusCode
        $call.Content = Get-ResponseText -Response $response
    } catch {
        $exception = $_.Exception
        $call.ErrorText = $exception.Message
        $code = 0
        try { $code = Get-StatusCodeFromException -Exception $exception } catch { $code = 0 }
        if ($code -gt 0) {
            $call.Code = $code
            try { $call.Content = Get-ErrorResponseBody -Exception $exception } catch { $call.Content = '' }
        } else {
            $call.Unreachable = $true
        }
    } finally {
        $stopwatch.Stop()
        $call.ElapsedMs = [int]$stopwatch.Elapsed.TotalMilliseconds
    }
    return $call
}

# ══════════════════════════════════════════════════════════════════════════════
# 响应摘要
# ══════════════════════════════════════════════════════════════════════════════
function Get-PropertyValue {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-EndpointSummary {
    param([string]$Name, [string]$Content, [string]$ContentType = '')

    $text = ''
    if ($null -ne $Content) { $text = [string]$Content }

    if ($Name -eq '/metrics') {
        $lineCount = @($text -split "`r?`n").Count
        $sampleCount = ([regex]::Matches($text, 'echomind_[a-zA-Z0-9_]+')).Count
        return ('text/plain; ' + $lineCount + ' 行; echomind_* 指标样本 ' + $sampleCount + ' 处')
    }

    $data = $null
    try {
        $data = $text | ConvertFrom-Json
    } catch {
        return ('非 JSON 响应: ' + (Get-ShortText $text 80))
    }

    if ($Name -eq '/health') {
        $status = '(无 status 字段)'
        $statusValue = Get-PropertyValue $data 'status'
        if ($null -ne $statusValue) { $status = [string]$statusValue }
        $agentCount = 0
        $agents = Get-PropertyValue $data 'agents'
        if ($null -ne $agents) { $agentCount = @($agents.PSObject.Properties).Count }
        return ('status=' + $status + '; agents 统计字段 ' + $agentCount + ' 个')
    }

    if ($Name -eq '/skills') {
        $count = '?'
        $countValue = Get-PropertyValue $data 'count'
        if ($null -ne $countValue) { $count = [string]$countValue }

        $errorCount = 0
        $errors = Get-PropertyValue $data 'errors'
        if ($null -ne $errors) { $errorCount = @($errors).Count }

        $firstName = ''
        $skills = Get-PropertyValue $data 'skills'
        if ($null -ne $skills -and @($skills).Count -gt 0) {
            $first = @($skills)[0]
            $nameValue = Get-PropertyValue $first 'name'
            if ($null -ne $nameValue) { $firstName = '; 首个=' + [string]$nameValue }
        }
        return ('count=' + $count + '; errors=' + $errorCount + $firstName)
    }

    if ($Name -eq '/knowledge/stats') {
        $chunks = '?'
        $chunksValue = Get-PropertyValue $data 'total_chunks'
        if ($null -ne $chunksValue) { $chunks = [string]$chunksValue }
        return ('total_chunks=' + $chunks)
    }

    if ($Name -eq '/monitor') {
        $fieldCount = @($data.PSObject.Properties).Count
        $alertCount = 0
        $alerts = Get-PropertyValue $data 'active_alerts'
        if ($null -ne $alerts) { $alertCount = @($alerts).Count }
        $suggestionCount = 0
        $suggestions = Get-PropertyValue $data 'suggestions'
        if ($null -ne $suggestions) { $suggestionCount = @($suggestions).Count }
        return ('字段 ' + $fieldCount + ' 个; 活跃告警 ' + $alertCount + ' 条; 优化建议 ' + $suggestionCount + ' 条')
    }

    if ($Name -eq '/chat') {
        $intent = ''
        $intentValue = Get-PropertyValue $data 'intent'
        if ($null -ne $intentValue) { $intent = [string]$intentValue }
        $agent = ''
        $agentValue = Get-PropertyValue $data 'agent_type'
        if ($null -ne $agentValue) { $agent = [string]$agentValue }
        $latency = ''
        $latencyValue = Get-PropertyValue $data 'latency_ms'
        if ($null -ne $latencyValue) { $latency = [string]$latencyValue }
        $reply = ''
        $replyValue = Get-PropertyValue $data 'response'
        if ($null -ne $replyValue) { $reply = Get-ShortText ([string]$replyValue) 60 }
        return ('intent=' + $intent + '; agent=' + $agent + '; latency_ms=' + $latency + '; 回复=' + $reply)
    }

    $topFields = @($data.PSObject.Properties).Count
    return ('JSON 对象; 顶层字段 ' + $topFields + ' 个')
}

# 非 2xx 时优先展示接口自己的错误信息（FastAPI 的 detail、SDK 的 error.message），
# 而不是按成功响应的字段去解析。
function Get-ErrorSummary {
    param([string]$Content, [string]$ErrorText)
    $text = ''
    if ($null -ne $Content) { $text = [string]$Content }

    if (-not [string]::IsNullOrEmpty($text)) {
        $data = $null
        try { $data = $text | ConvertFrom-Json } catch { $data = $null }
        if ($null -ne $data) {
            $detail = Get-PropertyValue $data 'detail'
            if ($null -ne $detail) {
                if ($detail -is [string]) { return (Get-ShortText ([string]$detail) 90) }
                try { return (Get-ShortText ($detail | ConvertTo-Json -Compress) 90) } catch { }
            }
            $errorField = Get-PropertyValue $data 'error'
            if ($null -ne $errorField -and $errorField -isnot [string]) {
                $errorType = Get-PropertyValue $errorField 'type'
                $errorMessage = Get-PropertyValue $errorField 'message'
                $parts = @()
                if ($null -ne $errorType) { $parts = $parts + [string]$errorType }
                if ($null -ne $errorMessage) { $parts = $parts + [string]$errorMessage }
                if ($parts.Count -gt 0) { return (Get-ShortText ($parts -join ': ') 90) }
            } elseif ($null -ne $errorField) {
                return (Get-ShortText ([string]$errorField) 90)
            }
            $messageField = Get-PropertyValue $data 'message'
            if ($null -ne $messageField) { return (Get-ShortText ([string]$messageField) 90) }
        }
        return (Get-ShortText $text 90)
    }

    if ([string]::IsNullOrEmpty($ErrorText)) { return '无响应内容' }
    return (Get-ShortText $ErrorText 90)
}

# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════
try {
    $script:PassCount = 0
    $script:FailCount = 0
    $script:SkipCount = 0
    $script:UnreachableCount = 0
    $script:TestedCount = 0
    $script:Records = @()

    $address = ''
    if ($null -ne $BaseUrl) { $address = $BaseUrl.Trim() }
    $address = $address.TrimEnd('/')
    if ([string]::IsNullOrEmpty($address) -or $address -notmatch '^https?://') {
        Write-Host ''
        Write-Host ('[ERROR] -BaseUrl 不合法: ' + $BaseUrl + '，示例: -BaseUrl http://localhost:8000') -ForegroundColor Red
        exit 3
    }

    $psVersion = $PSVersionTable.PSVersion.ToString()
    $psEditionName = 'Windows PowerShell'
    if ($PSVersionTable.ContainsKey('PSEdition')) { $psEditionName = [string]$PSVersionTable.PSEdition }

    $chatMode = '测试'
    if ($SkipChat) { $chatMode = '跳过（-SkipChat）' }

    Write-Host ''
    Write-Host ('=' * 82) -ForegroundColor DarkGray
    Write-Host ' EchoMind 接口冒烟测试 (smoke)' -ForegroundColor White
    Write-Host (' 目标地址  : ' + $address) -ForegroundColor DarkGray
    Write-Host (' PowerShell: ' + $psVersion + '  (' + $psEditionName + ')') -ForegroundColor DarkGray
    Write-Host (' /chat     : ' + $chatMode) -ForegroundColor DarkGray
    Write-Host (' 测试时间  : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray
    Write-Host ('=' * 82) -ForegroundColor DarkGray
    Write-Host (' 状态   接口                 状态码/耗时         摘要') -ForegroundColor DarkGray
    Write-Host ('-' * 82) -ForegroundColor DarkGray

    $checks = @(
        [pscustomobject]@{ Name = '/health';          Method = 'GET';  Path = '/health';          Body = '' },
        [pscustomobject]@{ Name = '/skills';          Method = 'GET';  Path = '/skills';          Body = '' },
        [pscustomobject]@{ Name = '/knowledge/stats'; Method = 'GET';  Path = '/knowledge/stats'; Body = '' },
        [pscustomobject]@{ Name = '/monitor';         Method = 'GET';  Path = '/monitor';         Body = '' },
        [pscustomobject]@{ Name = '/metrics';         Method = 'GET';  Path = '/metrics';         Body = '' },
        [pscustomobject]@{ Name = '/chat';            Method = 'POST'; Path = '/chat';            Body = '{"message":"Hello, what can you help me with?","user_id":"smoke-test"}' }
    )

    foreach ($check in $checks) {
        $isChat = ($check.Name -eq '/chat')

        if ($isChat -and $SkipChat) {
            $script:SkipCount = $script:SkipCount + 1
            $script:Records = $script:Records + ([pscustomobject]@{
                Name = $check.Name; Status = 'SKIP'; CodeText = '跳过'; ElapsedMs = 0; Summary = '未测试（-SkipChat）'
            })
            Write-Host ('[SKIP] ' + (Format-PadDisplay $check.Name 20) + ' ' + (Format-PadDisplay '跳过' 18) + ' 未测试（-SkipChat）') -ForegroundColor Yellow
            continue
        }

        $timeout = $TimeoutSec
        if ($isChat) { $timeout = $ChatTimeoutSec }

        $url = $address + $check.Path
        try {
            $call = Invoke-HttpCall -Method $check.Method -Url $url -TimeoutSec $timeout -Body $check.Body
        } catch {
            $call = [pscustomobject]@{
                Ok = $false; Code = 0; Content = ''; ErrorText = $_.Exception.Message; Unreachable = $false; ElapsedMs = 0
            }
        }
        $script:TestedCount = $script:TestedCount + 1

        $status = 'FAIL'
        $codeText = '不可达'
        $summaryText = ''
        $elapsedText = ('' + $call.ElapsedMs + 'ms')

        if ($call.Unreachable) {
            $script:UnreachableCount = $script:UnreachableCount + 1
            $script:FailCount = $script:FailCount + 1
            $reason = Get-ShortText $call.ErrorText 60
            if ([string]::IsNullOrEmpty($reason)) { $reason = '连接被拒绝或超时' }
            $summaryText = '服务不可达（' + $reason + '）'
        } elseif ($call.Ok -and $call.Code -ge 200 -and $call.Code -lt 300) {
            $status = 'PASS'
            $script:PassCount = $script:PassCount + 1
            $codeText = 'HTTP ' + $call.Code
            try {
                $summaryText = Get-EndpointSummary -Name $check.Name -Content $call.Content
            } catch {
                $summaryText = '摘要解析失败（已忽略）: ' + (Get-ShortText $_.Exception.Message 50)
            }
        } else {
            $script:FailCount = $script:FailCount + 1
            $codeText = 'HTTP ' + $call.Code
            $errorSummary = ''
            try {
                $errorSummary = Get-ErrorSummary -Content $call.Content -ErrorText $call.ErrorText
            } catch { $errorSummary = '' }
            if ([string]::IsNullOrEmpty($errorSummary)) { $errorSummary = '无响应内容' }
            $summaryText = $errorSummary
        }

        $script:Records = $script:Records + ([pscustomobject]@{
            Name = $check.Name; Status = $status; CodeText = $codeText; ElapsedMs = $call.ElapsedMs; Summary = $summaryText
        })

        $color = Get-ColorForStatus $status
        $statusText = '[' + $status + ']'
        $line = $statusText + ' ' + (Format-PadDisplay $check.Name 20) + ' ' +
                (Format-PadDisplay $codeText 12) + ' ' + (Format-PadDisplay $elapsedText 10) + ' ' + $summaryText
        Write-Host $line -ForegroundColor $color

        if ($isChat -and $status -eq 'FAIL') {
            Write-Host '        -> 提示: /chat 失败通常是 API Key 无效/未配置，或所选模型不存在；可先用 -SkipChat 验证其它接口' -ForegroundColor DarkGray
        }
        if ($status -eq 'FAIL' -and $call.Code -eq 503 -and -not $call.Unreachable) {
            Write-Host '        -> 提示: 503 表示服务进程已起但未就绪，等待 1-2 分钟后重试；查看日志 docker compose logs -f echomind' -ForegroundColor DarkGray
        }
    }

    # ── 汇总表 ──────────────────────────────────────────────────────────────
    Write-Host ('-' * 82) -ForegroundColor DarkGray
    Write-Host (' 结果   接口                 状态码      耗时        摘要') -ForegroundColor DarkGray
    foreach ($record in $script:Records) {
        $color = Get-ColorForStatus $record.Status
        $statusText = '[' + $record.Status + ']'
        $line = $statusText + ' ' + (Format-PadDisplay $record.Name 20) + ' ' +
                (Format-PadDisplay $record.CodeText 12) + ' ' +
                (Format-PadDisplay ('' + $record.ElapsedMs + 'ms') 10) + ' ' + (Get-ShortText $record.Summary 90)
        Write-Host $line -ForegroundColor $color
    }
    Write-Host ('-' * 82) -ForegroundColor DarkGray

    $summary = '汇总: PASS ' + $script:PassCount + ' 项 | FAIL ' + $script:FailCount + ' 项 | SKIP ' + $script:SkipCount + ' 项'
    Write-Host $summary -ForegroundColor $(if ($script:FailCount -gt 0) { 'Red' } elseif ($script:SkipCount -gt 0) { 'Yellow' } else { 'Green' })

    # ── 服务整体不可达 ──────────────────────────────────────────────────────
    if ($script:TestedCount -gt 0 -and $script:UnreachableCount -eq $script:TestedCount) {
        Write-Host ''
        Write-Host ('服务不可达: 无法连接 ' + $address) -ForegroundColor Red
        Write-Host '可能原因与排查顺序：' -ForegroundColor Yellow
        Write-Host '  1) 容器没启动        -> docker compose ps（看 echomind 是否 Up/healthy）' -ForegroundColor DarkGray
        Write-Host '  2) Docker 引擎没就绪 -> 先运行 scripts/preflight.ps1 检查引擎与端口' -ForegroundColor DarkGray
        Write-Host '  3) 端口没映射/被拦截 -> 默认 8000，可试 -BaseUrl http://localhost（Nginx 80 端口）' -ForegroundColor DarkGray
        Write-Host ''
        Write-Host '修复：docker compose up -d，等 echomind 变为 healthy 后重跑本脚本。' -ForegroundColor Yellow
        Write-Host ('退出码: 2（服务不可达）') -ForegroundColor DarkGray
        exit 2
    }

    if ($script:FailCount -gt 0) {
        Write-Host ''
        Write-Host '结论: 服务可访问，但存在失败接口（见上表 FAIL 行）。' -ForegroundColor Red
        Write-Host '      - 未就绪/503：等待启动完成后重试；日志: docker compose logs -f echomind' -ForegroundColor DarkGray
        Write-Host '      - /chat 失败：先确认 .env 里 ANTHROPIC_API_KEY 是真实 key 且模型名可用' -ForegroundColor DarkGray
        Write-Host '      - 只关心基础连通性时可加 -SkipChat 复测' -ForegroundColor DarkGray
        Write-Host ('退出码: 1（存在失败接口）') -ForegroundColor DarkGray
        exit 1
    }

    Write-Host ''
    Write-Host '结论: 冒烟测试全部通过。' -ForegroundColor Green
    if ($script:SkipCount -gt 0) {
        Write-Host '      注意: /chat 本次被跳过；配置真实 API Key 后建议去掉 -SkipChat 再跑一次。' -ForegroundColor Yellow
    }
    Write-Host ('退出码: 0') -ForegroundColor DarkGray
    exit 0
} catch {
    Write-Host ''
    Write-Host ('[ERROR] smoke 出现未预期异常（已捕获，不输出堆栈）: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '        请把上面这行连同目标地址、PowerShell 版本反馈给维护者。' -ForegroundColor DarkGray
    exit 3
}

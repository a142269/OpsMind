<#
.SYNOPSIS
    EchoMind 部署前置检查（Windows，PowerShell 5.1 / 7 通用）。

.DESCRIPTION
    在 docker compose up 之前逐项检查环境，只做只读检查，不会启动容器或修改文件：
      1) docker CLI 是否安装
      2) Docker 引擎是否连通（含 permission denied / 引擎未启动的区分）
      3) 是否具备 docker compose（Compose V2）
      4) 端口 80 / 8000 / 8001 / 6379 / 9090 是否被占用
      5) .env 是否存在、ANTHROPIC_API_KEY 是否仍是占位值
      6) 磁盘剩余空间（镜像 + ONNX 模型，< 15GB 警告）
      7) 模型配置是否与所选路线自洽
      8) Prometheus 告警规则 / Grafana 是否真的存在
    每项输出 PASS / WARN / FAIL（彩色），每个 FAIL 附带一行修复建议。

.PARAMETER Help
    显示帮助信息后退出。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1

.EXAMPLE
    pwsh -NoProfile -File .\scripts\preflight.ps1

.NOTES
    退出码：0 = 没有 FAIL（允许 WARN）；1 = 存在 FAIL；3 = 脚本自身异常（已捕获，不会抛裸堆栈）。
#>
[CmdletBinding()]
param(
    [switch]$Help
)

$ErrorActionPreference = 'Continue'

# ══════════════════════════════════════════════════════════════════════════════
# 帮助
# ══════════════════════════════════════════════════════════════════════════════
function Show-HelpText {
    Write-Host ''
    Write-Host 'EchoMind 部署前置检查 (preflight.ps1)' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '用法:' -ForegroundColor White
    Write-Host '  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\preflight.ps1'
    Write-Host '  pwsh -NoProfile -File .\scripts\preflight.ps1'
    Write-Host ''
    Write-Host '参数:' -ForegroundColor White
    Write-Host '  -Help    显示本帮助后退出'
    Write-Host ''
    Write-Host '检查项:' -ForegroundColor White
    Write-Host '  docker CLI / Docker 引擎连通性 / docker compose V2'
    Write-Host '  端口 80,8000,8001,6379,9090 / .env 与 API Key / 磁盘空间'
    Write-Host '  模型配置自洽性 / Prometheus 告警规则与 Grafana'
    Write-Host ''
    Write-Host '退出码: 0=无 FAIL(允许 WARN), 1=存在 FAIL, 3=脚本异常' -ForegroundColor DarkGray
    Write-Host ''
}

if ($Help) {
    Show-HelpText
    exit 0
}

# ══════════════════════════════════════════════════════════════════════════════
# 全局状态
# ══════════════════════════════════════════════════════════════════════════════
$script:RepoRoot    = ''
$script:PassCount   = 0
$script:WarnCount   = 0
$script:FailCount   = 0
$script:InfoCount   = 0
$script:Results     = @()

# ══════════════════════════════════════════════════════════════════════════════
# 输出辅助：按"显示宽度"对齐（中文占 2 列，避免表格错位）
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

# 从多行输出里挑出与关键字匹配的那一行（找不到时取最后一行），
# 避免把 docker info 的 "Client:" 这类首行当成错误原因展示。
function Get-KeyLine {
    param([string]$Text, [string]$Pattern = '', [int]$MaxLength = 120)
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $lines = @($Text -split "`r?`n")
    if (-not [string]::IsNullOrEmpty($Pattern)) {
        foreach ($line in $lines) {
            $content = [string]$line
            if ($content -match $Pattern -and -not [string]::IsNullOrWhiteSpace($content)) {
                return (Get-ShortText $content $MaxLength)
            }
        }
    }
    $last = ''
    foreach ($line in $lines) {
        if (-not [string]::IsNullOrWhiteSpace([string]$line)) { $last = [string]$line }
    }
    return (Get-ShortText $last $MaxLength)
}

# ══════════════════════════════════════════════════════════════════════════════
# 结果记录与打印
# ══════════════════════════════════════════════════════════════════════════════
function Add-CheckResult {
    param(
        [string]$Item,
        [string]$Status,
        [string]$Detail,
        [string]$Fix = ''
    )
    $result = New-Object PSObject
    $result | Add-Member -MemberType NoteProperty -Name Item   -Value $Item
    $result | Add-Member -MemberType NoteProperty -Name Status -Value $Status
    $result | Add-Member -MemberType NoteProperty -Name Detail -Value $Detail
    $result | Add-Member -MemberType NoteProperty -Name Fix    -Value $Fix

    $script:Results = $script:Results + $result

    switch ($Status) {
        'PASS' { $script:PassCount = $script:PassCount + 1 }
        'WARN' { $script:WarnCount = $script:WarnCount + 1 }
        'FAIL' { $script:FailCount = $script:FailCount + 1 }
        'INFO' { $script:InfoCount = $script:InfoCount + 1 }
    }

    $color = 'Gray'
    if ($Status -eq 'PASS') { $color = 'Green' }
    elseif ($Status -eq 'WARN') { $color = 'Yellow' }
    elseif ($Status -eq 'FAIL') { $color = 'Red' }
    elseif ($Status -eq 'INFO') { $color = 'Cyan' }

    $statusText = '[' + $Status + ']'
    $itemText = Format-PadDisplay $Item 18
    Write-Host ($statusText + ' ' + $itemText + ' ' + $Detail) -ForegroundColor $color

    if (-not [string]::IsNullOrEmpty($Fix)) {
        $fixLabel = '修复建议'
        if ($Status -eq 'INFO') { $fixLabel = '说明' }
        if ($Status -eq 'PASS') { $fixLabel = '备注' }
        Write-Host ('        -> ' + $fixLabel + ': ' + $Fix) -ForegroundColor DarkGray
    }
}

function Invoke-Check {
    param([string]$Item, [scriptblock]$Action)
    try {
        & $Action
    } catch {
        Add-CheckResult $Item 'FAIL' ('检查时出现异常（已捕获）: ' + (Get-ShortText $_.Exception.Message)) '请把该行输出反馈给维护者；脚本已跳过此项并继续检查其余项'
    }
}

# ══════════════════════════════════════════════════════════════════════════════
# 执行原生命令并捕获 stdout/stderr/退出码（跨 5.1 / 7 一致）
# ══════════════════════════════════════════════════════════════════════════════
function Invoke-NativeCommand {
    param(
        [string]$Executable,
        [string[]]$Arguments = @()
    )
    $exitCode = -1
    $text = ''
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & $Executable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($null -eq $exitCode) { $exitCode = 0 }
        $parts = @()
        foreach ($entry in @($raw)) {
            if ($null -eq $entry) { continue }
            if ($entry -is [System.Management.Automation.ErrorRecord]) {
                $msg = $entry.Exception.Message
                if (-not [string]::IsNullOrEmpty($msg)) { $parts = $parts + $msg }
                if ($null -ne $entry.Exception.InnerException) {
                    $inner = $entry.Exception.InnerException.Message
                    if (-not [string]::IsNullOrEmpty($inner)) { $parts = $parts + $inner }
                }
            } else {
                $parts = $parts + [string]$entry
            }
        }
        $text = ($parts -join "`n")
    } catch {
        $exitCode = -1
        $text = $_.Exception.Message
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ ExitCode = [int]$exitCode; Output = $text }
}

# ══════════════════════════════════════════════════════════════════════════════
# .env 解析
# ══════════════════════════════════════════════════════════════════════════════
function Get-EnvValue {
    param([string[]]$Lines, [string]$Key)
    if ($null -eq $Lines) { return $null }
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$'
    foreach ($line in $Lines) {
        if ([string]$line -match $pattern) {
            $value = $Matches[1].Trim()
            if ($value.Length -ge 2) {
                $quoted = (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                           ($value.StartsWith("'") -and $value.EndsWith("'")))
                if ($quoted) { $value = $value.Substring(1, $value.Length - 2) }
            }
            return $value
        }
    }
    return $null
}

function Test-PlaceholderValue {
    param([string]$Value)
    if ($null -eq $Value) { return $true }
    $value = $Value.Trim()
    if ($value.Length -eq 0) { return $true }
    $lower = $value.ToLowerInvariant()
    $exact = @('xxx', 'xxxx', 'xxx...', 'your_api_key', 'your-api-key', 'your_key',
               'changeme', 'change-me', '<your-key>', '<api-key>', 'sk-xxx',
               'sk-ant-xxx', 'todo', 'null', 'none', 'placeholder')
    if ($exact -contains $lower) { return $true }
    if ($lower -match '^x+$') { return $true }
    if ($lower -match 'your[_-]?(api)?[_-]?key|change[_-]?me|placeholder') { return $true }
    return $false
}

# ══════════════════════════════════════════════════════════════════════════════
# 具体检查项
# ══════════════════════════════════════════════════════════════════════════════
function Test-RepoLayout {
    $compose = Join-Path $script:RepoRoot 'docker-compose.yml'
    if (Test-Path -LiteralPath $compose) {
        Add-CheckResult '仓库目录' 'PASS' ('找到 compose 文件: ' + $compose) ''
    } else {
        Add-CheckResult '仓库目录' 'FAIL' ('未找到 ' + $compose) '请在 EchoMind 仓库根目录执行，或把 scripts/ 目录放回仓库内（本脚本需与 docker-compose.yml 同级目录）'
    }
}

function Test-DockerCli {
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        Add-CheckResult 'docker CLI' 'FAIL' '未找到 docker 命令' '安装 Docker Desktop（含 docker CLI）：https://www.docker.com/products/docker-desktop/ ，安装后重新打开终端'
        return
    }
    $version = Invoke-NativeCommand -Executable $command.Source -Arguments @('--version')
    if ($version.ExitCode -eq 0) {
        Add-CheckResult 'docker CLI' 'PASS' ((Get-ShortText $version.Output) + '  (' + $command.Source + ')') ''
    } else {
        Add-CheckResult 'docker CLI' 'FAIL' ('docker --version 执行失败: ' + (Get-ShortText $version.Output)) '重新安装 Docker Desktop，或检查 PATH 中 docker.exe 是否损坏'
    }
}

function Test-DockerEngine {
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        Add-CheckResult 'Docker 引擎' 'FAIL' 'docker CLI 不可用，无法检测引擎' '先安装 Docker Desktop，再重新运行本脚本'
        return
    }
    $info = Invoke-NativeCommand -Executable $command.Source -Arguments @('info')
    if ($info.ExitCode -eq 0) {
        $server = Invoke-NativeCommand -Executable $command.Source -Arguments @('version', '--format', '{{.Server.Version}}')
        $serverVersion = Get-ShortText $server.Output 40
        $detail = '引擎已连通'
        if (-not [string]::IsNullOrEmpty($serverVersion)) { $detail = $detail + '，Server ' + $serverVersion }
        Add-CheckResult 'Docker 引擎' 'PASS' $detail ''
        return
    }

    $text = ([string]$info.Output).ToLowerInvariant()

    if ($text -match 'permission denied|access is denied|拒绝访问') {
        $fix = 'Docker 引擎管道 permission denied。按顺序排查：① 打开 Docker Desktop，确认托盘鲸鱼图标状态为 "Engine running"；' +
               '② 管理员执行 wsl --update 后 wsl --shutdown，再重启 Docker Desktop（WSL2 后端必须可用）；' +
               '③ 管理员执行 net localgroup docker-users ' + $env:USERNAME + ' /add 把当前用户加入 docker-users 组，注销重登；' +
               '④ 仍失败则重启 Docker Desktop / 重启机器。'
        Add-CheckResult 'Docker 引擎' 'FAIL' ('docker info 失败：permission denied（' + (Get-KeyLine $info.Output 'permission denied|access is denied|拒绝访问' 130) + '）') $fix
        return
    }

    if ($text -match 'is the docker daemon running|error during connect|cannot connect|连接错误|pipe|管道') {
        Add-CheckResult 'Docker 引擎' 'FAIL' ('docker info 失败：引擎未运行或无法连接（' + (Get-KeyLine $info.Output 'error during connect|cannot connect|Is the docker daemon running|pipe|管道|拒绝' 130) + '）') '启动 Docker Desktop 并等待状态变为 Engine running；若使用 WSL2，先执行 wsl --update 并确认 wsl -l -v 中 docker-desktop 为 Running'
        return
    }

    Add-CheckResult 'Docker 引擎' 'FAIL' ('docker info 执行失败（exit=' + $info.ExitCode + '）：' + (Get-KeyLine $info.Output 'error|denied|fail|失败' 130)) '确认 Docker Desktop 已启动；仍失败请查看 Docker Desktop 的 Troubleshoot 日志，或重装 Docker Desktop'
}

function Test-ComposeV2 {
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        Add-CheckResult 'docker compose' 'FAIL' 'docker CLI 不可用，无法检测 Compose' '安装 Docker Desktop（自带 docker compose V2 插件）'
        return
    }
    $compose = Invoke-NativeCommand -Executable $command.Source -Arguments @('compose', 'version')
    if ($compose.ExitCode -eq 0) {
        Add-CheckResult 'docker compose' 'PASS' (Get-ShortText $compose.Output 80) ''
        return
    }
    $v1 = Get-Command docker-compose -ErrorAction SilentlyContinue
    if ($null -ne $v1) {
        Add-CheckResult 'docker compose' 'WARN' '只找到 Compose V1（docker-compose），缺少 V2 插件' '升级 Docker Desktop 以获得 docker compose（V2）；本项目所有命令均按 V2 编写，V1 不支持 depends_on.condition: service_healthy'
    } else {
        Add-CheckResult 'docker compose' 'FAIL' ('docker compose 不可用（exit=' + $compose.ExitCode + '）：' + (Get-ShortText $compose.Output)) '升级/重装 Docker Desktop（v2 自带 compose 插件），或安装 Docker Compose V2 插件'
    }
}

function Get-ListeningPorts {
    $listening = @()
    $netstat = & netstat -ano 2>$null
    foreach ($line in @($netstat)) {
        if ($null -eq $line) { continue }
        $text = [string]$line
        if ($text -match 'LISTENING' -and $text -match ':(\d+)\s') {
            $listening = $listening + [int]$Matches[1]
        }
    }
    return $listening
}

function Test-Ports {
    $targetPorts = @(80, 8000, 8001, 6379, 9090)
    $listening = Get-ListeningPorts
    $occupied = @()
    foreach ($port in $targetPorts) {
        if ($listening -contains $port) { $occupied = $occupied + $port }
    }
    if ($occupied.Count -eq 0) {
        Add-CheckResult '端口占用' 'PASS' ('80/8000/8001/6379/9090 全部空闲') ''
    } else {
        Add-CheckResult '端口占用' 'WARN' ('以下端口已被占用: ' + ($occupied -join ', ')) '若是本项目旧容器：docker compose down 释放；若是其它程序：结束占用进程，或修改 docker-compose.yml 的 ports 与 .env 后重启（注意 nginx 的 80 端口冲突最常见）'
    }
}

function Get-DriveSpace {
    param([string]$Root)
    try {
        $drive = New-Object System.IO.DriveInfo($Root)
        $result = New-Object PSObject
        $result | Add-Member -MemberType NoteProperty -Name Name   -Value $drive.Name
        $result | Add-Member -MemberType NoteProperty -Name FreeGB -Value ([math]::Round($drive.AvailableFreeSpace / 1GB, 1))
        $result | Add-Member -MemberType NoteProperty -Name TotalGB -Value ([math]::Round($drive.TotalSize / 1GB, 1))
        return $result
    } catch {
        return $null
    }
}

function Test-DiskSpace {
    $roots = @()
    try {
        $repoRoot = [System.IO.Path]::GetPathRoot($script:RepoRoot)
        if (-not [string]::IsNullOrEmpty($repoRoot)) { $roots = $roots + $repoRoot }
    } catch { }
    $cRoot = 'C:\'
    if (-not ($roots -contains $cRoot)) { $roots = $roots + $cRoot }

    $parts = @()
    $minFree = $null
    foreach ($root in $roots) {
        $space = Get-DriveSpace $root
        if ($null -eq $space) { continue }
        $parts = $parts + ($space.Name + ' 可用 ' + $space.FreeGB + 'GB / 共 ' + $space.TotalGB + 'GB')
        if ($null -eq $minFree) { $minFree = $space.FreeGB }
        elseif ($space.FreeGB -lt $minFree) { $minFree = $space.FreeGB }
    }

    if ($null -eq $minFree) {
        Add-CheckResult '磁盘空间' 'WARN' '无法读取磁盘剩余空间' '用 Get-PSDrive 手动确认系统盘与本仓库所在盘至少有 15GB 可用空间'
        return
    }

    $detail = $parts -join '；'
    if ($minFree -lt 15) {
        Add-CheckResult '磁盘空间' 'WARN' ($detail + '（最小可用 ' + $minFree + 'GB）') '镜像 + ONNX 模型 + 构建缓存约需 15GB：清理磁盘，或在 Docker Desktop 设置里把镜像存储位置换到空间更大的盘'
    } else {
        Add-CheckResult '磁盘空间' 'PASS' ($detail + '（最小可用 ' + $minFree + 'GB）') ''
    }
}

function Test-EnvFile {
    $envPath = Join-Path $script:RepoRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Add-CheckResult '.env 文件' 'FAIL' ('未找到 ' + $envPath) '复制模板并填写：Copy-Item .env.example .env；compose 的 env_file 缺少 .env 会直接启动失败'
        return $false
    }
    $lines = @()
    try {
        $lines = @(Get-Content -LiteralPath $envPath -Encoding UTF8 -ErrorAction Stop)
    } catch {
        Add-CheckResult '.env 文件' 'FAIL' ('.env 存在但读取失败: ' + (Get-ShortText $_.Exception.Message)) '检查文件是否被占用/权限不足；可重新执行 Copy-Item .env.example .env'
        return $false
    }
    Add-CheckResult '.env 文件' 'PASS' ('已找到 ' + $envPath + '（' + $lines.Count + ' 行）') ''

    $apiKey = Get-EnvValue -Lines $lines -Key 'ANTHROPIC_API_KEY'
    if (Test-PlaceholderValue $apiKey) {
        $shown = '(未设置)'
        if ($null -ne $apiKey -and $apiKey.Trim().Length -gt 0) { $shown = $apiKey.Trim() }
        Add-CheckResult 'API Key' 'FAIL' ('ANTHROPIC_API_KEY 仍是占位值 ' + $shown + '，服务能起但聊天全部失败') '打开 .env，把 ANTHROPIC_API_KEY 换成真实 key（Anthropic 官方 sk-ant-...，或 DeepSeek 控制台 sk-...）；不换 key 时 /chat /search /eval/run 必失败'
    } else {
        Add-CheckResult 'API Key' 'PASS' ('ANTHROPIC_API_KEY 已配置（内容已隐藏，长度 ' + $apiKey.Trim().Length + '）') ''
    }

    $model = Get-EnvValue -Lines $lines -Key 'ANTHROPIC_MODEL'
    $baseUrl = Get-EnvValue -Lines $lines -Key 'ANTHROPIC_BASE_URL'
    $baseShown = '(空 -> Anthropic 官方默认地址)'
    if (-not [string]::IsNullOrEmpty($baseUrl)) { $baseShown = $baseUrl }
    $modelShown = '(未设置，代码默认 claude-3-5-sonnet-20241022)'
    if (-not [string]::IsNullOrEmpty($model)) { $modelShown = $model }

    $detail = 'model=' + $modelShown + '; base_url=' + $baseShown
    $isDeepSeek = ($null -ne $baseUrl -and $baseUrl -match 'deepseek')
    $modelLooksFake = ($null -ne $model -and $model -notmatch '^deepseek-(chat|reasoner|coder)' -and $model -match 'deepseek-')
    if ($isDeepSeek -and $modelLooksFake) {
        Add-CheckResult '模型配置' 'WARN' ($detail + '（' + $model + ' 可能不是该账号可用模型）') 'DeepSeek 路线把 ANTHROPIC_MODEL 改为 deepseek-chat 或 deepseek-reasoner；Anthropic 官方路线请清空 ANTHROPIC_BASE_URL 并使用 claude-3-5-sonnet-20241022 等官方模型名'
    } else {
        Add-CheckResult '模型配置' 'PASS' $detail ''
    }
    return $true
}

function Test-MonitoringConfig {
    $alertsDir = Join-Path $script:RepoRoot 'config\alerts'
    $grafanaDir = Join-Path $script:RepoRoot 'config\grafana'
    $promConfig = Join-Path $script:RepoRoot 'config\prometheus.yml'

    $ruleCount = 0
    if (Test-Path -LiteralPath $alertsDir) {
        $ruleCount = @(Get-ChildItem -LiteralPath $alertsDir -Filter '*.yml' -ErrorAction SilentlyContinue).Count
    }
    $grafanaFiles = 0
    if (Test-Path -LiteralPath $grafanaDir) {
        $grafanaFiles = @(Get-ChildItem -LiteralPath $grafanaDir -Recurse -File -ErrorAction SilentlyContinue).Count
    }

    $mentionsRules = $false
    if (Test-Path -LiteralPath $promConfig) {
        try {
            $promText = Get-Content -LiteralPath $promConfig -Raw -Encoding UTF8 -ErrorAction Stop
            if ([string]$promText -match 'rule_files') { $mentionsRules = $true }
        } catch { }
    }

    if ($mentionsRules -and $ruleCount -eq 0) {
        Add-CheckResult '监控/告警' 'WARN' ('prometheus.yml 声明 rule_files=alerts/*.yml，但 alerts 目录下 0 个规则文件；config/grafana 有 ' + $grafanaFiles + ' 个文件且未在 compose 中挂载、没有 grafana 服务') '演示/宣传前先补齐：要么写 alerts/*.yml 并接入 Grafana，要么暂时从 config/prometheus.yml 删掉 rule_files 行；否则"有 Grafana/告警"的说法与实际不符'
    } elseif (-not $mentionsRules -and $ruleCount -eq 0) {
        Add-CheckResult '监控/告警' 'PASS' '未声明告警规则，配置与实际一致' ''
    } else {
        Add-CheckResult '监控/告警' 'PASS' ('告警规则文件 ' + $ruleCount + ' 个；grafana 相关文件 ' + $grafanaFiles + ' 个') ''
    }
}

function Show-OfflineHint {
    Add-CheckResult '离线构建' 'INFO' 'Dockerfile 构建期需要下载约 79MB ONNX 模型，无外网时 docker compose build 一定失败' '在可联网机器 docker compose build 后 docker save / docker load 到本机，详细步骤见 scripts/README.md 的「离线环境部署」'
}

# ══════════════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════════════
try {
    try {
        if (-not [string]::IsNullOrEmpty($PSScriptRoot)) {
            $script:RepoRoot = Split-Path -Parent $PSScriptRoot
        }
    } catch { }
    if ([string]::IsNullOrEmpty($script:RepoRoot)) { $script:RepoRoot = (Get-Location).Path }

    $psVersion = $PSVersionTable.PSVersion.ToString()
    $psEditionName = 'Windows PowerShell'
    if ($PSVersionTable.ContainsKey('PSEdition')) { $psEditionName = [string]$PSVersionTable.PSEdition }

    Write-Host ''
    Write-Host ('=' * 82) -ForegroundColor DarkGray
    Write-Host ' EchoMind 部署前置检查 (preflight)' -ForegroundColor White
    Write-Host (' 仓库目录  : ' + $script:RepoRoot) -ForegroundColor DarkGray
    Write-Host (' PowerShell: ' + $psVersion + '  (' + $psEditionName + ')') -ForegroundColor DarkGray
    Write-Host (' 检查时间  : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray
    Write-Host ('=' * 82) -ForegroundColor DarkGray
    Write-Host (' 状态   检查项             详情') -ForegroundColor DarkGray
    Write-Host ('-' * 82) -ForegroundColor DarkGray

    Invoke-Check '仓库目录'    { Test-RepoLayout }
    Invoke-Check 'docker CLI'  { Test-DockerCli }
    Invoke-Check 'Docker 引擎' { Test-DockerEngine }
    Invoke-Check 'docker compose' { Test-ComposeV2 }
    Invoke-Check '端口占用'    { Test-Ports }
    Invoke-Check '磁盘空间'    { Test-DiskSpace }

    try {
        [void](Test-EnvFile)
    } catch {
        Add-CheckResult '.env 文件' 'FAIL' ('检查 .env 时异常（已捕获）: ' + (Get-ShortText $_.Exception.Message)) '重新执行 Copy-Item .env.example .env 并填写真实 key'
    }

    Invoke-Check '监控/告警' { Test-MonitoringConfig }
    Show-OfflineHint

    # ── 汇总 ────────────────────────────────────────────────────────────────
    Write-Host ('-' * 82) -ForegroundColor DarkGray
    $summary = '汇总: PASS ' + $script:PassCount + ' 项 | WARN ' + $script:WarnCount + ' 项 | FAIL ' + $script:FailCount + ' 项'
    if ($script:InfoCount -gt 0) { $summary = $summary + ' | INFO ' + $script:InfoCount + ' 项' }
    if ($script:FailCount -gt 0) {
        Write-Host $summary -ForegroundColor Red
        Write-Host ''
        Write-Host '必须修复的 FAIL 项（按顺序处理）：' -ForegroundColor Yellow
        $index = 0
        foreach ($result in $script:Results) {
            if ($result.Status -eq 'FAIL') {
                $index = $index + 1
                Write-Host ('  ' + $index + '. [' + $result.Item + '] ' + $result.Detail) -ForegroundColor Red
                if (-not [string]::IsNullOrEmpty($result.Fix)) {
                    Write-Host ('     -> ' + $result.Fix) -ForegroundColor DarkGray
                }
            }
        }
        Write-Host ''
        Write-Host '结论: 环境未就绪，现在执行 docker compose up -d 大概率失败或"能起但聊天不可用"。' -ForegroundColor Red
        Write-Host '      修复上面的 FAIL 后重新运行本脚本；全部通过后再启动服务并跑 scripts/smoke.ps1。' -ForegroundColor DarkGray
        exit 1
    }

    Write-Host $summary -ForegroundColor Green
    if ($script:WarnCount -gt 0) {
        Write-Host '存在 WARN 项（不阻塞启动，但建议处理），见上面标记 [WARN] 的行。' -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host '结论: 环境检查通过，可以启动服务：' -ForegroundColor Green
    Write-Host '  1) docker compose up -d' -ForegroundColor DarkGray
    Write-Host '  2) docker compose ps        # 等 echomind 变为 healthy（首次启动约 1-2 分钟）' -ForegroundColor DarkGray
    Write-Host '  3) powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\smoke.ps1 -SkipChat' -ForegroundColor DarkGray
    exit 0
} catch {
    Write-Host ''
    Write-Host ('[ERROR] preflight 出现未预期异常（已捕获，不输出堆栈）: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '        请把上面这行连同 PowerShell 版本反馈给维护者。' -ForegroundColor DarkGray
    exit 3
}

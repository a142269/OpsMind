<#
.SYNOPSIS
    在线验证一键脚本：意图/对话评测 + 冒烟 + 证据落盘。
    适配 OpsMind（FastAPI, 默认 http://localhost:8000）。

.DESCRIPTION
    依次执行：
      1) GET  /health            服务与 Agent 统计
      2) GET  /skills            Skills 是否加载成功
      3) GET  /knowledge/stats   知识库片段数
      4) POST /knowledge/add     导入 data/demo_docs/sample_knowledge.json（幂等，已够则跳过）
      5) POST /chat              两轮对话，验证 LLM 通路与记忆写回
      6) GET  /monitor           监控摘要
      7) POST /eval/run          跑评测（默认使用 data/eval 中的 OpsMind 数据集）
    最后把「原始评测 JSON」与「人读 Markdown 报告」落到 logs/ 目录。

    重要：评测串行打 LLM，耗时可能 5~15 分钟。务必**直连 8000**，不要走 Nginx（其
    proxy_read_timeout 60s 会 504）。

.PARAMETER BaseUrl
    服务地址，默认 http://localhost:8000（直连，不要用 http://localhost）。

.PARAMETER EvalSet
    dataset     = data/eval 中全部意图与对话用例（默认，最慢）
    intent-only = 只跑数据集中的意图用例
    builtin     = 不传语料，跑项目内置 11 条（最快，仅供冒烟参考）

.PARAMETER SkipEval
    跳过评测（只做冒烟），适合先确认服务是否通。

.PARAMETER SkipKnowledgeImport
    跳过知识库导入。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_online_eval.ps1

.EXAMPLE
    # 只想快速确认服务通了
    powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_online_eval.ps1 -SkipEval

.NOTES
    兼容 PowerShell 5.1 与 PowerShell 7。所有输出中文。
#>

[CmdletBinding()]
param(
    [string]$BaseUrl = "http://localhost:8000",
    [ValidateSet("dataset", "intent-only", "builtin")]
    [string]$EvalSet = "dataset",
    [int]$ChatTimeoutSec = 120,
    [int]$EvalTimeoutSec = 1800,
    [switch]$SkipEval,
    [switch]$SkipKnowledgeImport
)

$ErrorActionPreference = "Continue"
$script:Results = @()
$script:Utf8 = New-Object System.Text.UTF8Encoding($false)

# ── 仓库根定位：脚本在 <root>/scripts/ 下 ────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir
$LogDir    = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$Stamp     = Get-Date -Format "yyyyMMdd_HHmmss"

# ── 输出辅助 ─────────────────────────────────────────────────────────────────
function Write-Head([string]$t) {
    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkGray
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkGray
}
function Write-Pass([string]$t) { Write-Host "[PASS] $t" -ForegroundColor Green }
function Write-Warn([string]$t) { Write-Host "[WARN] $t" -ForegroundColor Yellow }
function Write-Fail([string]$t) { Write-Host "[FAIL] $t" -ForegroundColor Red }
function Write-Info([string]$t) { Write-Host "[INFO] $t" -ForegroundColor Gray }

function Add-Result([string]$name, [string]$status, [string]$detail) {
    $script:Results += [pscustomobject]@{ 步骤 = $name; 结果 = $status; 说明 = $detail }
}

# ── HTTP 辅助：统一 UTF-8、超时、错误码、耗时 ────────────────────────────────
function Invoke-EchoApi {
    param(
        [string]$Path,
        [string]$Method = "GET",
        [object]$Body = $null,
        [int]$TimeoutSec = 60
    )
    $url = ($BaseUrl.TrimEnd("/")) + $Path
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("echomind_" + [guid]::NewGuid().ToString("N") + ".json")
    $sw  = [System.Diagnostics.Stopwatch]::StartNew()
    $out = [pscustomobject]@{
        Ok = $false; Status = 0; Ms = 0; Data = $null; Text = ""; Error = ""; Url = $url
    }
    try {
        $params = @{
            Uri             = $url
            Method          = $Method
            OutFile         = $tmp
            UseBasicParsing = $true
            TimeoutSec      = $TimeoutSec
        }
        if ($null -ne $Body) {
            $json = $Body | ConvertTo-Json -Depth 12 -Compress
            $params["Body"]        = $script:Utf8.GetBytes($json)
            $params["ContentType"] = "application/json; charset=utf-8"
        }
        Invoke-WebRequest @params | Out-Null
        $sw.Stop()
        $out.Ok     = $true
        $out.Status = 200
        $out.Ms     = [math]::Round($sw.Elapsed.TotalMilliseconds, 0)
        if (Test-Path $tmp) {
            $out.Text = [System.IO.File]::ReadAllText($tmp, [System.Text.Encoding]::UTF8)
            if ($out.Text.Trim().Length -gt 0) {
                try { $out.Data = $out.Text | ConvertFrom-Json } catch { $out.Data = $null }
            }
        }
    }
    catch {
        $sw.Stop()
        $out.Ms = [math]::Round($sw.Elapsed.TotalMilliseconds, 0)
        $out.Error = $_.Exception.Message
        if ($null -ne $_.Exception.Response) {
            try { $out.Status = [int]$_.Exception.Response.StatusCode } catch { $out.Status = -1 }
        }
        # 服务端错误响应体里通常有 detail，尽量读出来
        try {
            $stream = $_.Exception.Response.GetResponseStream()
            if ($null -ne $stream) {
                $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8)
                $out.Text = $reader.ReadToEnd()
                $reader.Close()
            }
        } catch { }
    }
    finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
    return $out
}

function Test-ServiceReachable {
    $r = Invoke-EchoApi -Path "/health" -TimeoutSec 10
    if (-not $r.Ok) {
        Write-Fail "服务不可达：$BaseUrl"
        Write-Info "排查顺序：1) docker compose ps 看 echomind 是否 Up(healthy)  2) docker compose logs --tail=80 echomind"
        Write-Info "          3) 确认没有走 Nginx（用 8000 直连）  4) 防火墙/端口占用"
        if ($r.Error) { Write-Info "底层错误：$($r.Error)" }
        return $false
    }
    return $true
}

# ══════════════════════════════════════════════════════════════════════════════
# 开始
# ══════════════════════════════════════════════════════════════════════════════
Write-Head "OpsMind 在线验证 · $Stamp"
Write-Info "目标服务：$BaseUrl"
Write-Info "仓库根  ：$RepoRoot"
Write-Info "评测量集：$EvalSet$(if ($SkipEval) { '（已跳过评测）' } else { '' })"

if (-not (Test-ServiceReachable)) {
    Add-Result "服务可达性" "FAIL" "无法连接 $BaseUrl"
    $script:Results | Format-Table -AutoSize
    exit 2
}
Add-Result "服务可达性" "PASS" $BaseUrl

# ── 1. /health ───────────────────────────────────────────────────────────────
Write-Head "1. GET /health"
$h = Invoke-EchoApi -Path "/health" -TimeoutSec 15
if ($h.Ok) {
    Write-Pass "/health 200 ($($h.Ms) ms)"
    if ($h.Data -and $h.Data.agents) {
        try { Write-Info ("agents 统计：" + ($h.Data.agents | ConvertTo-Json -Depth 4 -Compress)) } catch { }
    }
    Add-Result "/health" "PASS" "$($h.Ms) ms"
} else {
    Write-Fail "/health 失败 status=$($h.Status) $($h.Error)"
    Add-Result "/health" "FAIL" "status=$($h.Status)"
}

# ── 2. /skills ───────────────────────────────────────────────────────────────
Write-Head "2. GET /skills"
$s = Invoke-EchoApi -Path "/skills" -TimeoutSec 15
if ($s.Ok) {
    $cnt = "?"
    if ($s.Data -and $s.Data.PSObject.Properties.Name -contains "count") { $cnt = $s.Data.count }
    elseif ($s.Data -and $s.Data.PSObject.Properties.Name -contains "skills") { $cnt = $s.Data.skills.Count }
    Write-Pass "/skills 200 ($($s.Ms) ms)，Skill 数=$cnt"
    Add-Result "/skills" "PASS" "skills=$cnt"
} else {
    Write-Fail "/skills 失败 status=$($s.Status)"
    Add-Result "/skills" "FAIL" "status=$($s.Status)"
}

# ── 3. /knowledge/stats ──────────────────────────────────────────────────────
Write-Head "3. GET /knowledge/stats"
$k = Invoke-EchoApi -Path "/knowledge/stats" -TimeoutSec 20
$kbTotal = 0
if ($k.Ok) {
    if ($k.Data -and $k.Data.PSObject.Properties.Name -contains "total_chunks") { $kbTotal = [int]$k.Data.total_chunks }
    Write-Pass "/knowledge/stats 200，已索引片段=$kbTotal"
    Add-Result "/knowledge/stats" "PASS" "chunks=$kbTotal"
} else {
    Write-Fail "/knowledge/stats 失败 status=$($k.Status)"
    Add-Result "/knowledge/stats" "FAIL" "status=$($k.Status)"
}

# ── 4. 导入演示知识库 ────────────────────────────────────────────────────────
Write-Head "4. POST /knowledge/add（导入 demo 文档）"
$kbFile = Join-Path $RepoRoot "data\demo_docs\sample_knowledge.json"
if ($SkipKnowledgeImport) {
    Write-Info "已按参数跳过导入"
    Add-Result "知识库导入" "SKIP" "-SkipKnowledgeImport"
}
elseif (-not (Test-Path $kbFile)) {
    Write-Warn "找不到 $kbFile，跳过导入"
    Add-Result "知识库导入" "SKIP" "文件不存在"
}
elseif ($kbTotal -ge 6) {
    Write-Info "已有 $kbTotal 个片段（>=6），跳过重复导入"
    Add-Result "知识库导入" "SKIP" "已有 $kbTotal 片段"
}
else {
    try {
        $docs = [System.IO.File]::ReadAllText($kbFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $body = @{ documents = @($docs | ForEach-Object { @{ title = $_.title; content = $_.content } }) }
        $imp = Invoke-EchoApi -Path "/knowledge/add" -Method "POST" -Body $body -TimeoutSec 180
        if ($imp.Ok) {
            $added = 0; $tot = 0
            if ($imp.Data -and $imp.Data.PSObject.Properties.Name -contains "added_chunks") { $added = $imp.Data.added_chunks }
            if ($imp.Data -and $imp.Data.PSObject.Properties.Name -contains "total_chunks") { $tot   = $imp.Data.total_chunks }
            Write-Pass "导入成功：新增 $added 片段，总计 $tot（$($imp.Ms) ms）"
            Add-Result "知识库导入" "PASS" "added=$added total=$tot"
        } else {
            Write-Fail "导入失败 status=$($imp.Status) $($imp.Error)"
            Write-Info "提示：Chroma 首次启动要加载 embedding 模型，可能较慢；可稍后重试"
            Add-Result "知识库导入" "FAIL" "status=$($imp.Status)"
        }
    } catch {
        Write-Fail "读取/构造导入请求失败：$($_.Exception.Message)"
        Add-Result "知识库导入" "FAIL" "本地文件解析失败"
    }
}

# ── 5. /chat 两轮（验证 LLM 通路 + 记忆写回）─────────────────────────────────
Write-Head "5. POST /chat（两轮，验证 LLM 与记忆）"
$convId = "online_" + $Stamp
$turn1 = @{ message = "ERP 系统全公司都无法访问，已经影响财务结账"; user_id = "online_eval"; conv_id = $convId }
$c1 = Invoke-EchoApi -Path "/chat" -Method "POST" -Body $turn1 -TimeoutSec $ChatTimeoutSec
if ($c1.Ok -and $c1.Data) {
    $d = $c1.Data
    Write-Pass ("第 1 轮 200 ($($c1.Ms) ms)  意图=" + $d.intent + "  组=" + $d.intent_group +
                "  Agent=" + $d.agent_type + "  置信度=" + $d.intent_confidence)
    if ($d.PSObject.Properties.Name -contains "intent_degraded" -and $d.intent_degraded) {
        Write-Warn "intent_degraded=True —— 意图识别走了降级路径（LLM 可能异常）"
    }
    Write-Info ("路由原因：" + $d.routing_reason)
    Write-Info ("工具：" + (@($d.tools_used) -join ", ") + "  知识库命中=" + $d.knowledge_used + "  升级=" + $d.escalated)
    $preview = [string]$d.response
    if ($preview.Length -gt 220) { $preview = $preview.Substring(0, 220) + "..." }
    Write-Info ("回复预览：" + ($preview -replace "`r?`n", " "))
    Add-Result "/chat 第1轮" "PASS" ("intent=" + $d.intent + " agent=" + $d.agent_type + " " + $c1.Ms + "ms")

    $turn2 = @{ message = "请整理故障现象、影响范围和最近变更，升级值班工程师"; user_id = "online_eval"; conv_id = $convId }
    $c2 = Invoke-EchoApi -Path "/chat" -Method "POST" -Body $turn2 -TimeoutSec $ChatTimeoutSec
    if ($c2.Ok -and $c2.Data) {
        Write-Pass ("第 2 轮 200 ($($c2.Ms) ms)  意图=" + $c2.Data.intent + "  Agent=" + $c2.Data.agent_type)
        Add-Result "/chat 第2轮" "PASS" ("intent=" + $c2.Data.intent + " " + $c2.Ms + "ms")
    } else {
        Write-Fail "第 2 轮失败 status=$($c2.Status) $($c2.Error)"
        Add-Result "/chat 第2轮" "FAIL" "status=$($c2.Status)"
    }
} else {
    Write-Fail "第 1 轮失败 status=$($c1.Status) $($c1.Error)"
    if ($c1.Text) { Write-Info ("服务端返回：" + $c1.Text.Substring(0, [Math]::Min(300, $c1.Text.Length))) }
    Write-Info "最常见原因：ANTHROPIC_API_KEY 仍是占位值 xxx，或模型名不被账号支持"
    Add-Result "/chat 第1轮" "FAIL" "status=$($c1.Status)"
}

# ── 6. /monitor ──────────────────────────────────────────────────────────────
Write-Head "6. GET /monitor"
$m = Invoke-EchoApi -Path "/monitor" -TimeoutSec 20
if ($m.Ok) {
    Write-Pass "/monitor 200 ($($m.Ms) ms)"
    try {
        $txt = $m.Text
        if ($txt.Length -gt 900) { $txt = $txt.Substring(0, 900) + " ...(截断)" }
        Write-Info ($txt -replace "`r?`n", " ")
    } catch { }
    Add-Result "/monitor" "PASS" "$($m.Ms) ms"
} else {
    Write-Fail "/monitor 失败 status=$($m.Status)"
    Add-Result "/monitor" "FAIL" "status=$($m.Status)"
}

# ── 7. /eval/run ─────────────────────────────────────────────────────────────
$evalPath = $null
if ($SkipEval) {
    Write-Head "7. POST /eval/run —— 已跳过"
    Add-Result "评测" "SKIP" "-SkipEval"
}
else {
    Write-Head "7. POST /eval/run（$EvalSet）"
    Write-Info "评测串行调用 LLM，预计 3~15 分钟，请勿中断。超时上限 $EvalTimeoutSec 秒。"

    $evalBody = $null
    if ($EvalSet -ne "builtin") {
        $intentFile = Join-Path $RepoRoot "data\eval\intent_cases.json"
        $dialogFile = Join-Path $RepoRoot "data\eval\dialog_cases.json"
        if (-not (Test-Path $intentFile)) {
            Write-Fail "找不到 $intentFile，无法按数据集评测"
            Add-Result "评测" "FAIL" "数据集缺失"
        }
        else {
            try {
                $rawIntent = [System.IO.File]::ReadAllText($intentFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
                $intentCases = @($rawIntent | ForEach-Object { @{ message = $_.message; expected_intent = $_.expected_intent } })
                $evalBody = @{ intent_cases = $intentCases }
                if ($EvalSet -eq "dataset" -and (Test-Path $dialogFile)) {
                    $rawDialog = [System.IO.File]::ReadAllText($dialogFile, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
                    $dialogCases = @($rawDialog | ForEach-Object {
                        $one = @{}
                        if ($_.PSObject.Properties.Name -contains "question") { $one["question"] = $_.question }
                        if ($_.PSObject.Properties.Name -contains "turns")    { $one["turns"]    = @($_.turns) }
                        $one
                    })
                    $evalBody["dialog_cases"] = $dialogCases
                }
                Write-Info ("请求体：意图用例 " + $intentCases.Count + " 条" +
                            $(if ($evalBody.ContainsKey("dialog_cases")) { "，对话用例 " + $evalBody["dialog_cases"].Count + " 组" } else { "" }))
            } catch {
                Write-Fail "构造评测请求失败：$($_.Exception.Message)"
                Add-Result "评测" "FAIL" "请求体构造失败"
                $evalBody = "ABORT"
            }
        }
    }

    if ($evalBody -ne "ABORT") {
        if ($null -eq $evalBody) {
            $bodyToSend = @{}
            Write-Info "使用项目内置用例（不传语料）"
        } else {
            $bodyToSend = $evalBody
        }
        $ev = Invoke-EchoApi -Path "/eval/run" -Method "POST" -Body $bodyToSend -TimeoutSec $EvalTimeoutSec
        if ($ev.Ok -and $ev.Data) {
            $evalPath = Join-Path $LogDir ("online_eval_" + $Stamp + ".json")
            [System.IO.File]::WriteAllText($evalPath, $ev.Text, $script:Utf8)
            Write-Pass "评测完成 ($($ev.Ms) ms)，原始 JSON 已存：$evalPath"
            Add-Result "评测" "PASS" "$($ev.Ms) ms"
        } else {
            Write-Fail "评测失败 status=$($ev.Status) $($ev.Error)"
            if ($ev.Text) { Write-Info ("服务端返回前 400 字：" + $ev.Text.Substring(0, [Math]::Min(400, $ev.Text.Length))) }
            Write-Info "排查：1) 是否经 Nginx（改直连 8000）  2) key 是否有效  3) docker compose logs echomind"
            Add-Result "评测" "FAIL" "status=$($ev.Status)"
        }
    }
}

# ══════════════════════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════════════════════
Write-Head "汇总"

$pass = @($script:Results | Where-Object { $_.结果 -eq "PASS" }).Count
$fail = @($script:Results | Where-Object { $_.结果 -eq "FAIL" }).Count
$skip = @($script:Results | Where-Object { $_.结果 -eq "SKIP" }).Count

$script:Results | Format-Table -AutoSize

# ── 评测结果解读 + Markdown 报告 ─────────────────────────────────────────────
$mdPath = Join-Path $LogDir ("online_eval_" + $Stamp + ".md")
if ($evalPath -and (Test-Path $evalPath)) {
    $rep = [System.IO.File]::ReadAllText($evalPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add("# OpsMind 在线评测报告")
    $lines.Add("")
    $lines.Add("- 时间：$Stamp")
    $lines.Add("- 服务：$BaseUrl")
    $lines.Add("- 评测量集：$EvalSet")
    $lines.Add("- 原始 JSON：``$(Split-Path -Leaf $evalPath)``")
    $lines.Add("")
    $lines.Add("## 1. 总体")
    $lines.Add("")
    $lines.Add("| 指标 | 值 |")
    $lines.Add("|---|---|")
    $lines.Add("| pass_rate | $($rep.pass_rate) |")
    $lines.Add("| passed / total | $($rep.passed) / $($rep.total) |")
    $lines.Add("| degraded_count | $(if ($null -ne $rep.degraded_count) { $rep.degraded_count } else { 'n/a' }) |")
    $lines.Add("| degraded_rate  | $(if ($null -ne $rep.degraded_rate) { $rep.degraded_rate } else { 'n/a' }) |")
    $lines.Add("")
    $lines.Add("> **判读规则**：``degraded_rate > 0`` 表示有 Judge 解析失败、分数被 0.5 兜底，")
    $lines.Add("> 此时 ``pass_rate`` **不可当作质量结论**。degraded_rate 越高，这份报告越不可信。")
    $lines.Add("")

    # 意图评测
    $intentRes = @($rep.results | Where-Object { $_.test_id -eq "intent_recognition" })
    if ($intentRes.Count -gt 0) {
        $ir = $intentRes[0]
        $lines.Add("## 2. 意图识别")
        $lines.Add("")
        if ($ir.scores) {
            foreach ($p in $ir.scores.PSObject.Properties) {
                $lines.Add("- ``$($p.Name)`` = $($p.Value)")
            }
        }
        if ($ir.detail) { $lines.Add("- detail: $($ir.detail)") }
        $lines.Add("")

        # per_class
        if ($ir.metadata -and ($ir.metadata.PSObject.Properties.Name -contains "per_class")) {
            $lines.Add("### per-class（P/R/F1/support）")
            $lines.Add("")
            $lines.Add("| 意图 | support | P | R | F1 |")
            $lines.Add("|---|---:|---:|---:|---:|")
            $best = @(); $worst = @()
            foreach ($p in $ir.metadata.per_class.PSObject.Properties) {
                $v = $p.Value
                $lines.Add("| $($p.Name) | $($v.support) | $($v.precision) | $($v.recall) | $($v.f1) |")
                $best  += [pscustomobject]@{ k = $p.Name; f1 = [double]$v.f1; sup = [int]$v.support }
                $worst += [pscustomobject]@{ k = $p.Name; f1 = [double]$v.f1; sup = [int]$v.support }
            }
            $lines.Add("")
            $lines.Add("**F1 最高 3 类**：" + (($best | Sort-Object f1 -Descending | Select-Object -First 3 | ForEach-Object { "$($_.k)($($_.f1))" }) -join "、"))
            $lines.Add("")
            $lines.Add("**F1 最低 3 类**：" + (($worst | Sort-Object f1 | Select-Object -First 3 | ForEach-Object { "$($_.k)($($_.f1))" }) -join "、"))
            $lines.Add("")
        }
    }

    # 对话评测
    $dlg = @($rep.results | Where-Object { $_.test_id -ne "intent_recognition" })
    if ($dlg.Count -gt 0) {
        $dlgPass = @($dlg | Where-Object { $_.passed }).Count
        $lines.Add("## 3. 对话质量（LLM-as-Judge）")
        $lines.Add("")
        $lines.Add("- 用例数：$($dlg.Count)，通过：$dlgPass")
        $degraded = @($dlg | Where-Object { $_.metadata -and ($_.metadata.PSObject.Properties.Name -contains "judge_degraded") -and $_.metadata.judge_degraded })
        $lines.Add("- judge 降级（解析失败）用例：$($degraded.Count)")
        $lines.Add("")
        $lines.Add("| test_id | passed | overall | degraded |")
        $lines.Add("|---|---|---|---|")
        foreach ($d in $dlg) {
            $ov = ""
            if ($d.scores -and ($d.scores.PSObject.Properties.Name -contains "overall")) { $ov = $d.scores.overall }
            $dg = ""
            if ($d.metadata -and ($d.metadata.PSObject.Properties.Name -contains "judge_degraded")) { $dg = $d.metadata.judge_degraded }
            $lines.Add("| $($d.test_id) | $($d.passed) | $ov | $dg |")
        }
        $lines.Add("")
    }

    # 建议
    if ($rep.recommendations -and @($rep.recommendations).Count -gt 0) {
        $lines.Add("## 4. 系统给出的优化建议")
        $lines.Add("")
        foreach ($r in $rep.recommendations) { $lines.Add("- $r") }
        $lines.Add("")
    }

    # 冒烟记录
    $lines.Add("## 5. 冒烟记录")
    $lines.Add("")
    $lines.Add("| 步骤 | 结果 | 说明 |")
    $lines.Add("|---|---|---|")
    foreach ($r in $script:Results) { $lines.Add("| $($r.步骤) | $($r.结果) | $($r.说明) |") }
    $lines.Add("")

    [System.IO.File]::WriteAllText($mdPath, ($lines -join "`r`n"), $script:Utf8)

    # 终端摘要
    Write-Head "评测核心数字"
    Write-Host ("  pass_rate      : " + $rep.pass_rate) -ForegroundColor White
    Write-Host ("  passed/total   : " + $rep.passed + " / " + $rep.total) -ForegroundColor White
    if ($null -ne $rep.degraded_rate) {
        $dr = [double]$rep.degraded_rate
        if ($dr -gt 0) { Write-Host ("  degraded_rate  : " + $dr + "   <-- Judge 有失败，pass_rate 不可信") -ForegroundColor Red }
        else           { Write-Host ("  degraded_rate  : " + $dr + "   <-- 测量有效") -ForegroundColor Green }
    }
    if ($intentRes.Count -gt 0 -and $intentRes[0].scores) {
        foreach ($p in $intentRes[0].scores.PSObject.Properties) {
            Write-Host ("  intent." + $p.Name + " = " + $p.Value) -ForegroundColor Cyan
        }
    }
    Write-Head "已落盘"
    Write-Host "  原始 JSON : $evalPath" -ForegroundColor White
    Write-Host "  人读报告  : $mdPath"   -ForegroundColor White
    Write-Info "把这两份文件发回即可，无需截图。"
}
elseif (-not $SkipEval) {
    Write-Warn "没有拿到评测结果，未生成 Markdown 报告。"
}

Write-Head "结束"
Write-Host ("  通过 $pass | 失败 $fail | 跳过 $skip") -ForegroundColor White
if ($fail -gt 0) { exit 1 } else { exit 0 }

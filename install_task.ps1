<#
.SYNOPSIS
    把 cn-broker-api 装成「登录时触发」的计划任务，或卸掉它。

.DESCRIPTION
    为什么是计划任务，而不是 Windows 服务、不是容器、也不是启动文件夹：

    - **不能做 Windows 服务、不能进容器**：这个服务要枚举窗口、往密码框里打字、抓窗口位图，
      而 Windows 服务跑在会话 0、容器里根本看不到桌面。它必须跑在**交互桌面会话**里。
    - **不用启动文件夹**：计划任务有「上次运行结果」这一栏（配置写错了退出码就是 2，
      一眼能看见），能用 schtasks /run 手动触发一次，也不会被谁清理开始菜单时顺手带走。

    刻意**不勾**「以最高权限运行」。目标客户端是非提权进程，往它的窗口发消息不需要提权
    （提权只在反方向才是必需的）；而提权跑的话，本服务启动的客户端会继承高完整性级别，
    与人平时手动启动的那一份不是同一套环境。

    刻意**不做任何 DPI 声明**。客户端是 DPI 不感知进程，本服务保持不感知，
    GetWindowRect、PrintWindow 位图、我们 Post 的客户区坐标才落在同一套坐标里。

.PARAMETER ConfigPath
    TOML 配置文件路径。给了就同时写成**用户级**环境变量 CN_BROKER_API_CONFIG——
    计划任务在登录时拿的是用户环境块，所以这是唯一能让它稳定生效的办法。
    不给就用默认位置（仓库上一级的 cn-broker-api.toml）。

.PARAMETER Start
    装完立刻跑一次，省得为了验证去注销重登。

.PARAMETER Uninstall
    删掉这个计划任务。不动配置、不动状态目录、不动那个用户级环境变量。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -ConfigPath D:\Agent\cn-broker-api.toml -Start

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'cn-broker-api',
    [string]$ConfigPath = '',
    [string]$Python = '',
    [switch]$Start,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Me = "$env:USERDOMAIN\$env:USERNAME"

function Write-Step($msg) { Write-Host "==> $msg" }

# ── 卸载 ────────────────────────────────────────────────────────────
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Step "计划任务 '$TaskName' 本来就不存在，没有要做的事"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Step "已删除计划任务 '$TaskName'"
    Write-Host "配置、状态目录、CN_BROKER_API_CONFIG 环境变量都没动 —— 要清就自己来"
    exit 0
}

# ── 找解释器 ────────────────────────────────────────────────────────
# 默认用仓库自带的 venv。pythonw.exe 而不是 python.exe：登录时不该弹一个黑框出来，
# 而日志本来就写文件（见 __main__.py 的 _init_logging）。
if ($Python -ne '') {
    $exe = $Python
} else {
    $exe = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
}
if (-not (Test-Path -LiteralPath $exe)) {
    throw "找不到解释器：$exe`n先建虚拟环境：python -m venv .venv 再 .venv\Scripts\python -m pip install -r requirements.txt"
}

# ── 配置 ────────────────────────────────────────────────────────────
if ($ConfigPath -ne '') {
    if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "配置文件不存在：$ConfigPath" }
    $ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
    Write-Step "写用户级环境变量 CN_BROKER_API_CONFIG = $ConfigPath"
    [Environment]::SetEnvironmentVariable('CN_BROKER_API_CONFIG', $ConfigPath, 'User')
    $env:CN_BROKER_API_CONFIG = $ConfigPath
}

# 🔴 pythonw 没有控制台，配置读不出来那行 stderr 会被丢掉，能看见的只有计划任务
#    「上次运行结果 = 2」。所以在这里先用带控制台的解释器把配置读一遍，
#    让错误现在就以人话的形式出现，而不是等到某天早上服务不在。
# ⭐ 走 `-m cn_broker_api.config` 而不是 `python -c "..."`：后者要在这里嵌一串带引号的
#    Python 源码，引号会被 PowerShell 的原生参数解析吃掉，报出来是 `SyntaxError`——
#    看着像配置坏了。这个脚本从写出来到 2026-08-22 一次都没跑通过，就是栽在这上面。
$checker = Join-Path (Split-Path -Parent $exe) 'python.exe'
if (Test-Path -LiteralPath $checker) {
    Write-Step '先把配置读一遍'
    Push-Location $RepoRoot
    try {
        & $checker -m cn_broker_api.config
        if ($LASTEXITCODE -ne 0) { throw "配置读不过（退出码 $LASTEXITCODE），先把配置改对再装任务" }
    } finally {
        Pop-Location
    }
}

# ── 注册 ────────────────────────────────────────────────────────────
$action = New-ScheduledTaskAction -Execute $exe -Argument '-m cn_broker_api' -WorkingDirectory $RepoRoot

# 登录后延迟 30 秒：登录那一刻磁盘和网络都在抢，而这个服务一点都不着急——
# 它是被动的，第一次真调用来自调用方 09:00 那条任务。
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $Me
$trigger.Delay = 'PT30S'

# ExecutionTimeLimit = 0 ＝ 不限时长（它是常驻进程，默认那个 3 天上限会把它杀掉）。
# MultipleInstances IgnoreNew ＝ 手动 schtasks /run 一次不会起出第二个来抢端口。
# RestartCount ＝ 崩了自己回来；这台机器上没人盯着它。
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

# LogonType Interactive ＝ 跑在交互桌面会话里。**这一条是硬要求**：
# 勾成「不管用户是否登录都运行」会把它扔进会话 0，那里没有桌面、没有窗口，
# 整套 Win32 自动化一个动作都做不成。
$principal = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Write-Step "计划任务 '$TaskName' 已存在，覆盖注册（幂等）"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description 'cn-broker-api：本机券商接入服务。必须跑在交互桌面会话里（要枚举窗口、抓位图）。' | Out-Null

Write-Step "已注册 '$TaskName'"
Write-Host "  可执行   $exe -m cn_broker_api"
Write-Host "  工作目录 $RepoRoot"
Write-Host "  触发     登录后 30 秒"

if ($Start) {
    Write-Step '现在跑一次'
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "  上次运行结果 $($info.LastTaskResult)（0 ＝ 正在跑；2 ＝ 配置有问题）"
}

Write-Host ''
Write-Host '常用命令：'
Write-Host "  手动跑一次 schtasks /run   /tn $TaskName"
Write-Host "  停掉       schtasks /end   /tn $TaskName"
Write-Host "  看上次结果 schtasks /query /tn $TaskName /v /fo LIST"
Write-Host "  卸掉       powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Uninstall"
Write-Host ''
Write-Host '[!] 装完不等于验过：打开诊断页 http://127.0.0.1/ 对应端口，确认四项检查真的答话了。' -ForegroundColor Yellow

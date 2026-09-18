param(
    [switch]$NoBrowser,
    [switch]$SelfTest,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000
)

# 芜湖 12345 智慧助手 Windows 图形启动器。
# 双击项目根目录“启动芜湖政务助手.cmd”即可运行。
# 关闭本窗口时，仅结束由本启动器创建的前端和后端进程树。

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WebRoot = Join-Path $ProjectRoot "web"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$NextCli = Join-Path $WebRoot "node_modules\next\dist\bin\next"
$StandaloneRoot = Join-Path $WebRoot ".next\standalone"
$StandaloneServer = Join-Path $StandaloneRoot "server.js"
$LogDirectory = Join-Path $ProjectRoot "logs\desktop-launcher"
$LocalEmbeddingModel = Join-Path $ProjectRoot "models\bge-small-zh-v1.5"
$BackendUrl = "http://127.0.0.1:$BackendPort"
$AppUrl = "http://127.0.0.1:$FrontendPort"
$script:BackendProcess = $null
$script:FrontendProcess = $null
$script:Started = $false
$script:StatusLabel = $null
$script:InstanceMutex = $null

# 普通桌面启动只允许一个控制窗口。重复双击时保留已运行的服务，
# 仅打开已有工作台，避免第二个窗口因端口占用而显示“启动失败”。
if (-not $SelfTest) {
    $createdNew = $false
    $script:InstanceMutex = [System.Threading.Mutex]::new($true, "Local\Wuhu12345DesktopLauncher", [ref]$createdNew)
    if (-not $createdNew) {
        if (-not $NoBrowser) {
            try { Start-Process $AppUrl } catch { }
        }
        exit 0
    }
}

function Set-Status([string]$Text, [System.Drawing.Color]$Color) {
    if ($null -eq $script:StatusLabel) { return }
    $script:StatusLabel.Text = $Text
    $script:StatusLabel.ForeColor = $Color
    [System.Windows.Forms.Application]::DoEvents()
}

function Get-NodeExecutable {
    $candidates = @(
        (Join-Path ${env:ProgramFiles} "nodejs\node.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "nodejs\node.exe"),
        (Get-Command node.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    return $candidates | Select-Object -First 1
}

function Get-PortOwner([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $connection) { return $null }
    $process = Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Pid = $connection.OwningProcess
        Name = if ($process) { $process.ProcessName } else { "未知进程" }
    }
}

function Assert-PortAvailable([int]$Port, [string]$ServiceName) {
    $owner = Get-PortOwner $Port
    if ($owner) {
        throw "$ServiceName 需要端口 $Port，但该端口正被 $($owner.Name)（PID $($owner.Pid)）占用。为避免关闭其他程序，启动器未执行任何清理；请先关闭占用端口的旧服务后再试。"
    }
}

function Stop-ProcessTree($Process) {
    if ($null -eq $Process) { return }
    try {
        if (-not $Process.HasExited) {
            $taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
            & $taskkill /PID $Process.Id /T /F 2>$null | Out-Null
        }
    } catch {
        # 退出过程以“尽力停止”处理，避免窗口关闭时出现二次错误提示。
    }
}

function Stop-WuhuServices {
    Stop-ProcessTree $script:FrontendProcess
    Stop-ProcessTree $script:BackendProcess
    $script:FrontendProcess = $null
    $script:BackendProcess = $null
    $script:Started = $false
}

function Wait-ForHttp([string]$Url, [string]$DisplayName, $Process, [int]$TimeoutSeconds = 45) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            throw "$DisplayName 启动后意外退出。请点击【打开日志目录】查看错误日志。"
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return }
        } catch {
            # 服务刚启动时连接失败是正常现象，继续等待。
        }
        Set-Status "正在等待${DisplayName}就绪…" ([System.Drawing.Color]::FromArgb(164, 112, 20))
        Start-Sleep -Milliseconds 700
        [System.Windows.Forms.Application]::DoEvents()
    }
    throw "$DisplayName 在 $TimeoutSeconds 秒内没有就绪。请点击【打开日志目录】检查日志。"
}

function Start-WuhuServices {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "未找到 Python 虚拟环境：$Python。请先按项目 README 完成一次依赖安装。"
    }
    if (-not (Test-Path -LiteralPath $NextCli)) {
        throw "未找到前端依赖：$NextCli。请先在 web 目录执行一次 npm install。"
    }
    $node = Get-NodeExecutable
    if (-not $node) {
        throw "未找到 Node.js。请安装 Node.js 22 LTS 后重新双击启动器。"
    }

    Assert-PortAvailable $BackendPort "后端服务"
    Assert-PortAvailable $FrontendPort "前端服务"
    New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null

    # 桌面模式不依赖 Mongo/Redis，但优先使用随项目提供的本地 BGE 语义模型；
    # 仅当模型目录缺失时才降级为 hash，避免演示版本与正式 RAG 使用不同检索能力。
    $env:STORAGE_MODE = "memory"
    $env:VECTOR_BACKEND = "memory"
    $env:PI_AGENT_ENABLED = "false"
    $env:BACKEND_URL = $BackendUrl
    if (Test-Path -LiteralPath $LocalEmbeddingModel) {
        $env:EMBEDDING_PROVIDER = "local"
        $env:EMBEDDING_MODEL = $LocalEmbeddingModel
        $env:EMBEDDING_ALLOW_HASH_FALLBACK = "false"
    } else {
        $env:EMBEDDING_PROVIDER = "hash"
        $env:EMBEDDING_ALLOW_HASH_FALLBACK = "true"
    }

    $backendOut = Join-Path $LogDirectory "backend.out.log"
    $backendErr = Join-Path $LogDirectory "backend.err.log"
    $frontendOut = Join-Path $LogDirectory "frontend.out.log"
    $frontendErr = Join-Path $LogDirectory "frontend.err.log"
    Remove-Item -LiteralPath $backendOut, $backendErr, $frontendOut, $frontendErr -Force -ErrorAction SilentlyContinue

    Set-Status "正在启动后端服务…" ([System.Drawing.Color]::FromArgb(21, 101, 82))
    $script:BackendProcess = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
    # 首次加载本地 BGE 并构建官方文档向量索引可能需要一分钟以上，保持 GUI 可见并等待真实就绪。
    Wait-ForHttp "$BackendUrl/healthz" "后端服务" $script:BackendProcess 150

    Set-Status "正在启动前端工作台…" ([System.Drawing.Color]::FromArgb(21, 101, 82))
    if (Test-Path -LiteralPath $StandaloneServer) {
        # Next.js standalone 产物不会自动携带静态资源；桌面启动前同步到运行目录。
        $standaloneStatic = Join-Path $StandaloneRoot ".next\static"
        $rootStatic = Join-Path $WebRoot ".next\static"
        if (Test-Path -LiteralPath $rootStatic) {
            New-Item -ItemType Directory -Force -Path (Join-Path $StandaloneRoot ".next") | Out-Null
            Copy-Item -LiteralPath $rootStatic -Destination $standaloneStatic -Recurse -Force
        }
        $publicRoot = Join-Path $WebRoot "public"
        if (Test-Path -LiteralPath $publicRoot) {
            Copy-Item -LiteralPath $publicRoot -Destination (Join-Path $StandaloneRoot "public") -Recurse -Force
        }
        $env:PORT = "$FrontendPort"
        $env:HOSTNAME = "127.0.0.1"
        $script:FrontendProcess = Start-Process -FilePath $node `
            -ArgumentList @($StandaloneServer) -WorkingDirectory $StandaloneRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
    } else {
        # 尚未构建时允许开发模式作为兜底；日常工作人员使用已构建的 standalone 版本。
        $script:FrontendProcess = Start-Process -FilePath $node `
            -ArgumentList @($NextCli, "dev", "-p", "$FrontendPort", "-H", "127.0.0.1") `
            -WorkingDirectory $WebRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
    }
    Wait-ForHttp $AppUrl "前端工作台" $script:FrontendProcess
    $script:Started = $true
}

if ($SelfTest) {
    try {
        Start-WuhuServices
        Write-Output "desktop-launcher-self-test-ok"
    } finally {
        Stop-WuhuServices
    }
    exit
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "芜湖 12345 智慧助手"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object System.Drawing.Size(610, 345)
$form.MinimumSize = $form.Size
$form.MaximumSize = $form.Size
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.ShowInTaskbar = $true
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(247, 250, 249)
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10)

$title = New-Object System.Windows.Forms.Label
$title.Text = "芜湖 12345 智慧助手"
$title.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 21, [System.Drawing.FontStyle]::Bold)
$title.ForeColor = [System.Drawing.Color]::FromArgb(20, 76, 65)
$title.AutoSize = $true
$title.Location = New-Object System.Drawing.Point(38, 32)
$form.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = "本机受理与工单协同工作台"
$subtitle.AutoSize = $true
$subtitle.ForeColor = [System.Drawing.Color]::FromArgb(84, 111, 103)
$subtitle.Location = New-Object System.Drawing.Point(41, 78)
$form.Controls.Add($subtitle)

$panel = New-Object System.Windows.Forms.Panel
$panel.Location = New-Object System.Drawing.Point(38, 116)
$panel.Size = New-Object System.Drawing.Size(516, 86)
$panel.BackColor = [System.Drawing.Color]::White
$panel.BorderStyle = "FixedSingle"
$form.Controls.Add($panel)

$script:StatusLabel = New-Object System.Windows.Forms.Label
$script:StatusLabel.Text = "准备启动本地服务…"
$script:StatusLabel.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11, [System.Drawing.FontStyle]::Bold)
$script:StatusLabel.ForeColor = [System.Drawing.Color]::FromArgb(21, 101, 82)
$script:StatusLabel.AutoSize = $true
$script:StatusLabel.Location = New-Object System.Drawing.Point(20, 17)
$panel.Controls.Add($script:StatusLabel)

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "关闭本窗口会同步停止本启动器创建的前端与后端服务。"
$hint.AutoSize = $true
$hint.ForeColor = [System.Drawing.Color]::FromArgb(96, 120, 113)
$hint.Location = New-Object System.Drawing.Point(20, 51)
$panel.Controls.Add($hint)

$openButton = New-Object System.Windows.Forms.Button
$openButton.Text = "打开工作台"
$openButton.Enabled = $false
$openButton.Size = New-Object System.Drawing.Size(156, 42)
$openButton.Location = New-Object System.Drawing.Point(38, 228)
$openButton.BackColor = [System.Drawing.Color]::FromArgb(15, 125, 103)
$openButton.ForeColor = [System.Drawing.Color]::White
$openButton.FlatStyle = "Flat"
$openButton.Add_Click({ Start-Process $AppUrl })
$form.Controls.Add($openButton)

$logButton = New-Object System.Windows.Forms.Button
$logButton.Text = "打开日志目录"
$logButton.Size = New-Object System.Drawing.Size(156, 42)
$logButton.Location = New-Object System.Drawing.Point(211, 228)
$logButton.Add_Click({ New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null; Start-Process explorer.exe -ArgumentList $LogDirectory })
$form.Controls.Add($logButton)

$closeButton = New-Object System.Windows.Forms.Button
$closeButton.Text = "关闭系统并退出"
$closeButton.Size = New-Object System.Drawing.Size(156, 42)
$closeButton.Location = New-Object System.Drawing.Point(384, 228)
$closeButton.Add_Click({ $form.Close() })
$form.Controls.Add($closeButton)

$form.Add_Shown({
    try {
        Start-WuhuServices
        Set-Status "系统已启动，可开始办理工单" ([System.Drawing.Color]::FromArgb(21, 101, 82))
        $openButton.Enabled = $true
        $form.Activate()
        if (-not $NoBrowser) { Start-Process $AppUrl }
    } catch {
        Stop-WuhuServices
        New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
        $_ | Out-File -LiteralPath (Join-Path $LogDirectory "launcher.err.log") -Encoding utf8
        Set-Status "启动失败，请查看日志或提示信息" ([System.Drawing.Color]::FromArgb(180, 38, 38))
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, "芜湖 12345 智慧助手", "OK", "Error") | Out-Null
    }
})

$form.Add_FormClosing({ Stop-WuhuServices })
try {
    [void]$form.ShowDialog()
} finally {
    Stop-WuhuServices
    if ($null -ne $script:InstanceMutex) {
        try { $script:InstanceMutex.ReleaseMutex() } catch { }
        $script:InstanceMutex.Dispose()
    }
}

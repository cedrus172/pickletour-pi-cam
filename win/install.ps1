# PiTailscale Windows installer.
# Cách dùng: bấm chuột phải "Run with PowerShell as Administrator".
# Nếu PS block script, chạy trước: Set-ExecutionPolicy -Scope Process Bypass -Force

$ErrorActionPreference = "Stop"

function Say($msg)  { Write-Host "» $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "✓ $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "! $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

# --- Admin check ---
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent() `
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Die "Script phải chạy as Administrator" }

$ROOT    = $PSScriptRoot
$INSTALL = "C:\PiTailscale"
$PYEXE   = ""
$PORT    = 9999
$TASK    = "PiTailscale"

Say "1/6 — Kiểm tra Python 3..."
$pyCandidates = @("python", "python3", "py", "C:\Python312\python.exe", "C:\Python311\python.exe")
foreach ($c in $pyCandidates) {
    try {
        $v = & $c --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $v -match "Python 3") {
            $PYEXE = (Get-Command $c).Source
            Ok "Python: $v → $PYEXE"
            break
        }
    } catch {}
}
if (-not $PYEXE) {
    Warn "Chưa có Python. Tải python-3.12 (chọn 'Add to PATH') rồi chạy lại:"
    Warn "  winget install -e --id Python.Python.3.12"
    Die  "Thiếu Python 3"
}

Say "2/6 — Kiểm tra Nmap..."
$nmapPaths = @(
    "C:\Program Files (x86)\Nmap\nmap.exe",
    "C:\Program Files\Nmap\nmap.exe"
)
$nmapOk = $false
foreach ($p in $nmapPaths) { if (Test-Path $p) { $nmapOk = $true; Ok "Nmap: $p"; break } }
if (-not $nmapOk) {
    try { & nmap --version | Out-Null; if ($LASTEXITCODE -eq 0) { $nmapOk = $true; Ok "Nmap trong PATH" } } catch {}
}
if (-not $nmapOk) {
    Warn "Chưa có Nmap. Cài tự động:"
    try {
        winget install -e --id Insecure.Nmap --accept-source-agreements --accept-package-agreements
        Ok "Nmap installed"
    } catch {
        Warn "winget fail. Tải thủ công: https://nmap.org/download.html#windows"
    }
}

Say "3/6 — Kiểm tra Tailscale..."
$tsPaths = @("C:\Program Files\Tailscale\tailscale.exe", "C:\Program Files (x86)\Tailscale\tailscale.exe")
$tsOk = $false
foreach ($p in $tsPaths) { if (Test-Path $p) { $tsOk = $true; Ok "Tailscale: $p"; break } }
if (-not $tsOk) {
    Warn "Chưa có Tailscale. Cài & đăng nhập:"
    Warn "  winget install -e --id Tailscale.Tailscale"
    Warn "Sau đó mở Tailscale, đăng nhập account."
}

Say "4/6 — Copy source → $INSTALL + tạo venv..."
New-Item -ItemType Directory -Path $INSTALL -Force | Out-Null
New-Item -ItemType Directory -Path "$INSTALL\data" -Force | Out-Null
Copy-Item -Path "$ROOT\*.py", "$ROOT\requirements.txt" -Destination $INSTALL -Force
Copy-Item -Path "$ROOT\templates" -Destination $INSTALL -Recurse -Force
Copy-Item -Path "$ROOT\static"    -Destination $INSTALL -Recurse -Force

if (-not (Test-Path "$INSTALL\.venv")) {
    & $PYEXE -m venv "$INSTALL\.venv"
}
& "$INSTALL\.venv\Scripts\pip.exe" install --quiet --upgrade pip
& "$INSTALL\.venv\Scripts\pip.exe" install --quiet -r "$INSTALL\requirements.txt"
Ok "venv + deps ready"

Say "5/6 — Mở firewall cho port $PORT (TCP)..."
try {
    netsh advfirewall firewall delete rule name="PiTailscale $PORT" | Out-Null
} catch {}
netsh advfirewall firewall add rule name="PiTailscale $PORT" `
    dir=in action=allow protocol=TCP localport=$PORT | Out-Null
Ok "firewall rule added"

Say "6/6 — Đăng ký Task Scheduler (At startup, user SYSTEM)..."
# pythonw.exe chạy không có console window
$runner = "$INSTALL\.venv\Scripts\pythonw.exe"
$script = "$INSTALL\app.py"

# Xoá task cũ nếu có
schtasks /Query /TN $TASK 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $TASK /F | Out-Null }

$action = "`"$runner`" `"$script`""
schtasks /Create /TN $TASK /TR $action /SC ONSTART /RU "SYSTEM" /RL HIGHEST /F | Out-Null
Ok "task '$TASK' — chạy khi Windows boot"

# Khởi động luôn lần này
Say "Khởi động lần đầu..."
schtasks /Run /TN $TASK | Out-Null
Start-Sleep -Seconds 3

# Test port
$test = Test-NetConnection -ComputerName 127.0.0.1 -Port $PORT -WarningAction SilentlyContinue
if ($test.TcpTestSucceeded) {
    Ok "App đang chạy tại http://localhost:$PORT"
} else {
    Warn "App chưa phản hồi trên port $PORT. Check log:"
    Warn "  Get-EventLog -LogName Application -Newest 20 | Where Source -Like 'PiTailscale*'"
    Warn "Hoặc chạy tay: $runner $script"
}

Write-Host ""
Ok "Cài xong!"
Write-Host "  UI local      : http://localhost:$PORT"
Write-Host "  UI Tailscale  : http://<tailscale-ip>:$PORT"
Write-Host "  Install dir   : $INSTALL"
Write-Host "  Data dir      : $INSTALL\data"
Write-Host "  Stop service  : schtasks /End /TN $TASK"
Write-Host "  Restart       : schtasks /End /TN $TASK ; schtasks /Run /TN $TASK"
Write-Host "  Uninstall     : .\uninstall.ps1"

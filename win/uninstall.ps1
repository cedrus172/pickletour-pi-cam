# PiTailscale Windows uninstaller.
# Chạy chuột phải → Run with PowerShell as Administrator.
$ErrorActionPreference = "Continue"

$TASK = "PiTailscale"
$INSTALL = "C:\PiTailscale"
$PORT = 9999

Write-Host "Dừng task..." -ForegroundColor Cyan
schtasks /End /TN $TASK 2>$null | Out-Null
schtasks /Delete /TN $TASK /F 2>$null | Out-Null

Write-Host "Xoá firewall rule..." -ForegroundColor Cyan
netsh advfirewall firewall delete rule name="PiTailscale $PORT" 2>$null | Out-Null

$ans = Read-Host "Xoá luôn $INSTALL (gồm data/forwards.db)? [y/N]"
if ($ans -eq "y" -or $ans -eq "Y") {
    Remove-Item -Path $INSTALL -Recurse -Force
    Write-Host "✓ Đã xoá $INSTALL" -ForegroundColor Green
} else {
    Write-Host "Giữ lại $INSTALL" -ForegroundColor Yellow
}
Write-Host "Uninstall xong." -ForegroundColor Green

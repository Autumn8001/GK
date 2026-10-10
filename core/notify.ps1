param(
    [string]$Title = "Grok 注册",
    [string]$Message = "请在浏览器窗口完成 Cloudflare 人机验证"
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

try {
    $ni = New-Object System.Windows.Forms.NotifyIcon
    $ni.Icon = [System.Drawing.SystemIcons]::Warning
    $ni.BalloonTipTitle = $Title
    $ni.BalloonTipText = $Message
    $ni.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
    $ni.Visible = $true
    $ni.ShowBalloonTip(15000)
    Start-Sleep -Seconds 9
    $ni.Dispose()
} catch {
}

try {
    1..3 | ForEach-Object {
        [console]::beep(1000, 180)
        Start-Sleep -Milliseconds 120
    }
} catch {
}

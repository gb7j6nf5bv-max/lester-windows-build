# Lester for Windows: first-run setup and start. Lester.exe runs this hidden.
# The Windows twin of the first half of start_backend.sh.
#   1. Installs Lester in your user folder (no admin needed) with Start menu and Desktop shortcuts,
#      so it keeps working after the download is deleted.
#   2. Installs uv (Python manager) and the iPhone tools (pymobiledevice3, Python 3.13) once.
#   3. Starts lester_win.py, which shows the window and starts the iPhone tunnel.
$ErrorActionPreference = 'Stop'
$AppDir   = $PSScriptRoot                                  # ...\Lester\app
$Root     = Split-Path $AppDir -Parent                     # ...\Lester (holds Lester.exe)
$Install  = Join-Path $env:LOCALAPPDATA 'Programs\Lester'
$State    = Join-Path $env:LOCALAPPDATA 'Lester'
$Log      = Join-Path $State 'lester.log'
New-Item -ItemType Directory -Force -Path $State | Out-Null
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

function Write-Log($msg) { Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) }
function Fail($msg) {
  Write-Log "alert: $msg"
  [System.Windows.Forms.MessageBox]::Show($msg, "Lester couldn't start", 'OK', 'Error') | Out-Null
  exit 1
}
$script:Note = $null
function Notify($msg) {
  # Like a Mac notification: a balloon from the taskbar's corner
  if (-not $script:Note) {
    $script:Note = New-Object System.Windows.Forms.NotifyIcon
    $ico = Join-Path $AppDir 'lester.ico'
    $script:Note.Icon = if (Test-Path $ico) { New-Object System.Drawing.Icon $ico } else { [System.Drawing.SystemIcons]::Information }
    $script:Note.Text = 'Lester'
    $script:Note.Visible = $true
  }
  $script:Note.ShowBalloonTip(15000, 'Lester', $msg, 'Info')
}
function Running {
  try { (Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/ping' -TimeoutSec 2).ok -eq $true } catch { $false }
}

Write-Log "=== Lester setup ($Root) ==="

# 1. Install into the user's Programs folder, then run from there
if ((Resolve-Path $Root).Path.TrimEnd('\') -ne $Install.TrimEnd('\')) {
  if (Running) {
    # An older Lester is open: quit it so its files can be replaced
    try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/quit' -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null } catch {}
    Start-Sleep -Seconds 3
  }
  try {
    New-Item -ItemType Directory -Force -Path $Install | Out-Null
    Copy-Item -Path (Join-Path $Root '*') -Destination $Install -Recurse -Force
    Get-ChildItem -Path $Install -Recurse -File | Unblock-File   # downloaded files carry a "from the internet" mark
  } catch { Fail "Couldn't install Lester into your user folder ($($_.Exception.Message))." }
  $shell = New-Object -ComObject WScript.Shell
  foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
    try {
      $lnk = $shell.CreateShortcut((Join-Path $dir 'Lester.lnk'))
      $lnk.TargetPath = Join-Path $Install 'Lester.exe'
      $lnk.WorkingDirectory = $Install
      $lnk.IconLocation = (Join-Path $Install 'app\lester.ico') + ',0'
      $lnk.Description = "Change your iPhone's location"
      $lnk.Save()
    } catch { Write-Log "shortcut: $($_.Exception.Message)" }
  }
  Write-Log "installed to $Install"
  Start-Process -FilePath (Join-Path $Install 'Lester.exe')
  exit 0
}

# 2. uv, then the iPhone tools. Both live in %LOCALAPPDATA%\Lester and touch nothing else.
$Bin   = Join-Path $State 'bin'
$Uv    = Join-Path $Bin 'uv.exe'
$Venv  = Join-Path $State 'tools\pymobiledevice3'
$Py    = Join-Path $Venv 'Scripts\python.exe'
$PyW   = Join-Path $Venv 'Scripts\pythonw.exe'
$Pmd3  = Join-Path $Venv 'Scripts\pymobiledevice3.exe'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not (Test-Path $Uv)) {
  Notify 'Setting up Lester for the first time. This takes about a minute.'
  $zip = Join-Path $env:TEMP ("lester-uv-{0}.zip" -f [guid]::NewGuid())
  try {
    New-Item -ItemType Directory -Force -Path $Bin | Out-Null
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $Bin -Force
  } catch { Fail "Couldn't download the setup files. Check your internet connection and open Lester again." }
  finally { Remove-Item $zip -ErrorAction SilentlyContinue }
  if (-not (Test-Path $Uv)) { Fail "Couldn't set up Lester. Open Lester again to retry." }
}

$toolsOk = $false
if ((Test-Path $Py) -and (Test-Path $Pmd3)) {
  # Windows PowerShell treats anything a program prints as an error when 'Stop' is on, so relax it here
  $ErrorActionPreference = 'Continue'
  & $Py -c 'import sys; sys.exit(sys.version_info < (3, 13))' 2>$null
  $toolsOk = ($LASTEXITCODE -eq 0)
  $ErrorActionPreference = 'Stop'
}
if (-not $toolsOk) {
  Notify 'Installing iPhone tools...'
  $env:UV_TOOL_DIR = Join-Path $State 'tools'
  $env:UV_TOOL_BIN_DIR = $Bin
  $env:UV_PYTHON_INSTALL_DIR = Join-Path $State 'python'
  $env:UV_NO_MODIFY_PATH = '1'
  $ErrorActionPreference = 'Continue'   # uv prints its progress on stderr
  & $Uv tool install --python 3.13 --force pymobiledevice3 *>> $Log
  $uvCode = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($uvCode -ne 0 -or -not (Test-Path $Pmd3)) {
    Fail "Couldn't install the iPhone tools. Check your internet connection and open Lester again."
  }
}
if ($script:Note) { $script:Note.Dispose() }

# 3. The window, the map server and the tunnel
Write-Log "iPhone tools: $Pmd3"
Start-Process -FilePath $PyW -ArgumentList @('-I', "`"$(Join-Path $AppDir 'lester_win.py')`"") -WorkingDirectory $AppDir
exit 0

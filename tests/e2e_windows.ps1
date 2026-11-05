# End-to-end test of Lester for Windows, the way a person uses it: unzip, double-click Lester.exe.
# Runs ON WINDOWS only (GitHub's Windows build machine). No iPhone needed; never moves a phone.
# It installs Lester for the current user, so don't run it on your own PC.
# Results go to $env:RUNNER_TEMP\lester-results (log, screenshot); exits 1 if any check fails.
$ErrorActionPreference = 'Stop'
$Repo    = Split-Path $PSScriptRoot -Parent
$Results = Join-Path $env:RUNNER_TEMP 'lester-results'
$Pkg     = Join-Path $env:RUNNER_TEMP 'unzipped\Lester'
$Install = Join-Path $env:LOCALAPPDATA 'Programs\Lester'
$State   = Join-Path $env:LOCALAPPDATA 'Lester'
New-Item -ItemType Directory -Force -Path $Results, (Join-Path $Pkg 'app') | Out-Null
$script:failed = @()
$Summary = Join-Path $Results 'summary.txt'
function Note($line) { Write-Host $line; Add-Content -Path $Summary -Value $line -Encoding UTF8 }
function Check($name, $ok, $detail = '') {
  if ($ok) { Note "PASS $name" } else { Note "FAIL $name $detail"; $script:failed += $name }
}
function Wait-Until([scriptblock]$cond, [int]$seconds) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Get-Date) -lt $end) { try { if (& $cond) { return $true } } catch {} ; Start-Sleep -Seconds 2 }
  return $false
}
function Up($url) { try { Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3 | Out-Null; $true } catch { $false } }
function Lester-Processes {
  Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match 'lester_win\.py|lester\.py|tunnel_win\.py|--app=http://127\.0\.0\.1:8765') }
}
function Shot($name) {
  try {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
    $bmp.Save((Join-Path $Results "$name.png")); $g.Dispose(); $bmp.Dispose()
    Write-Host "screenshot: $name.png ($($b.Width)x$($b.Height))"
  } catch { Write-Host "no screenshot: $($_.Exception.Message)" }
}

# 0. The programs don't need extra Microsoft files (VCRUNTIME140.dll)
foreach ($exe in 'Lester.exe', 'lester-sideload.exe') {
  $bytes = [IO.File]::ReadAllBytes((Join-Path $Repo "build\windows-bin\$exe"))
  $text = [Text.Encoding]::ASCII.GetString($bytes)
  Check "$exe needs no VCRUNTIME140.dll" (-not ($text -match '(?i)vcruntime140'))
}

# 1. The download, unzipped like a person would (no iPhone app file in this test)
Copy-Item (Join-Path $Repo 'build\windows-bin\Lester.exe') $Pkg
Copy-Item (Join-Path $Repo 'build\windows-bin\lester-sideload.exe') (Join-Path $Pkg 'app')
foreach ($f in 'lester.py', 'index.html', 'app\lester_stream.py', 'app\lester_pair.py', 'app\tunnel_win.py', 'app\lester_win.py', 'app\setup_win.ps1') {
  Copy-Item (Join-Path $Repo $f) (Join-Path $Pkg 'app')
}

# 2. First open: installs, downloads the iPhone tools, opens the window and starts the tunnel
$t0 = Get-Date
Start-Process -FilePath (Join-Path $Pkg 'Lester.exe')
$serverUp = Wait-Until { Up 'http://127.0.0.1:8765/api/ping' } 600
Check 'first open: map server starts (installs everything first)' $serverUp
Note ("first open took {0:n0} s" -f ((Get-Date) - $t0).TotalSeconds)
Check 'installed into the user Programs folder' (Test-Path (Join-Path $Install 'Lester.exe'))
Check 'Start menu shortcut' (Test-Path (Join-Path ([Environment]::GetFolderPath('Programs')) 'Lester.lnk'))
Check 'Desktop shortcut' (Test-Path (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Lester.lnk'))
Check 'uv installed' (Test-Path (Join-Path $State 'bin\uv.exe'))
Check 'iPhone tools installed' (Test-Path (Join-Path $State 'tools\pymobiledevice3\Scripts\pymobiledevice3.exe'))
Check 'iOS 17+ tunnel running (admin prompt accepted)' (Wait-Until { Up 'http://127.0.0.1:49151/' } 90)
Check 'Lester window open (Edge app mode)' (Wait-Until { @(Lester-Processes | Where-Object { $_.Name -eq 'msedge.exe' }).Count -gt 0 } 30)
$procs = Lester-Processes
Note ("Lester processes: " + (($procs | ForEach-Object { "$($_.Name): " + $_.CommandLine.Substring(0, [Math]::Min(160, $_.CommandLine.Length)) }) -join ' | '))
# Map pictures come from OpenStreetMap, which blocks many data-centre computers like this one
try { $code = (Invoke-WebRequest -UseBasicParsing 'https://tile.openstreetmap.org/3/4/2.png' -TimeoutSec 10).StatusCode } catch { $code = $_.Exception.Response.StatusCode.value__ }
Note "info: OpenStreetMap answers this build machine with HTTP $code (403 = this machine is blocked, not Lester)"
Check 'server runs from the installed copy' (@($procs | Where-Object { $_.CommandLine -match [regex]::Escape($Install) -and $_.CommandLine -match 'lester\.py' }).Count -gt 0)
Start-Sleep -Seconds 6
Shot 'lester-window'

if ($serverUp) {
  $page = (Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8765/').Content
  Check 'page: Windows wording' ($page.Contains('this PC') -and -not $page.Contains('this Mac'))
  $st = Invoke-RestMethod 'http://127.0.0.1:8765/api/status'
  Check 'status: no iPhone, asks for Apple Devices' ($st.appleDevicesMissing -eq $true)
  $setup = Invoke-RestMethod 'http://127.0.0.1:8765/api/iphone-setup?udid=00000000-0000000000000000'
  Check 'setup guide answers' ($setup.os -eq 'windows' -and $setup.connected -eq $false)
  Check 'setup guide can install the iPhone app helper' ($null -ne $setup.canInstall)
  $tile = try { (Invoke-WebRequest -UseBasicParsing 'https://tile.openstreetmap.org/3/4/2.png' -Headers @{ Referer = 'http://127.0.0.1:8765/' } -UserAgent 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0' -TimeoutSec 10).StatusCode } catch { $_.Exception.Response.StatusCode.value__ }
  Check 'map pictures load with the referrer Lester now sends' ($tile -eq 200) $tile
  $py = Join-Path $State 'tools\pymobiledevice3\Scripts\python.exe'
  $out = & $py -I (Join-Path $Install 'app\lester_pair.py') '00000000-0000000000000000' 'Lester' (Join-Path $env:RUNNER_TEMP 'k.plist') 2>&1 | Out-String
  Check 'pairing tool reports a missing iPhone cleanly' ($out -match '^ERROR')
}

# 3. Quitting (the map's power button) stops everything
try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/api/quit' -ContentType 'application/json' -Body '{}' | Out-Null } catch {}
Check 'quit: map server stops' (Wait-Until { -not (Up 'http://127.0.0.1:8765/api/ping') } 30)
Check 'quit: tunnel stops' (Wait-Until { -not (Up 'http://127.0.0.1:49151/') } 40)
Check 'quit: window and helpers close' (Wait-Until { @(Lester-Processes).Count -eq 0 } 40) (Lester-Processes | ForEach-Object { $_.Name } | Out-String)

# 4. Opening it again from the Start menu copy: no reinstall, quick start
$t1 = Get-Date
Start-Process -FilePath (Join-Path $Install 'Lester.exe')
Check 'second open: starts again' (Wait-Until { Up 'http://127.0.0.1:8765/api/ping' } 120)
Note ("second open took {0:n0} s" -f ((Get-Date) - $t1).TotalSeconds)
Check 'second open: tunnel again' (Wait-Until { Up 'http://127.0.0.1:49151/' } 90)
# Opening it while it's already open just shows the window
Start-Process -FilePath (Join-Path $Install 'Lester.exe'); Start-Sleep -Seconds 15
# (Windows lists each Python program twice: a small starter and the real one; count the starters' children once)
$servers = @(Lester-Processes | Where-Object { $_.CommandLine -match 'lester\.py' -and $_.CommandLine -notmatch 'lester_win' })
$serverIds = $servers | ForEach-Object { $_.ProcessId }
Check 'opening twice keeps one server' (@($servers | Where-Object { $serverIds -notcontains $_.ParentProcessId }).Count -eq 1) ($servers.Count)
Shot 'lester-window-again'
# Closing the window quits Lester
Lester-Processes | Where-Object { $_.Name -eq 'msedge.exe' -and $_.CommandLine -notmatch '--type=' } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
Check 'closing the window quits Lester' (Wait-Until { -not (Up 'http://127.0.0.1:8765/api/ping') } 40)
Check 'closing the window stops the tunnel' (Wait-Until { -not (Up 'http://127.0.0.1:49151/') } 40)

# Logs for reading afterwards
foreach ($log in 'lester.log', 'tunneld.log') {
  $p = Join-Path $State $log
  if (Test-Path $p) { Copy-Item $p (Join-Path $Results $log) }
}
Write-Host '--- lester.log (end) ---'
if (Test-Path (Join-Path $State 'lester.log')) { Get-Content (Join-Path $State 'lester.log') -Tail 60 }
Write-Host ''
if ($script:failed.Count) { Note "$($script:failed.Count) failed: $($script:failed -join ', ')"; exit 1 }
Note 'All end-to-end checks passed'
exit 0

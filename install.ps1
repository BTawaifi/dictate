<#
.SYNOPSIS
  One-step setup for Dictate: creates a private Python environment, installs the
  dependencies and downloads the speech model (~490 MB, once).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Startup   # also start at login
#>
[CmdletBinding()]
param([switch]$Startup, [switch]$SkipModel)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$py = $null
foreach ($candidate in @(@('py', '-3'), @('python'))) {
    $cmd = Get-Command $candidate[0] -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $extra = @($candidate | Select-Object -Skip 1)
    $ErrorActionPreference = 'Continue'   # Windows PowerShell 5.1 turns native stderr into errors under 'Stop'
    $version = & $cmd.Source @extra -c 'import sys; print(*sys.version_info[:2], sep=chr(46))' 2>$null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = 'Stop'
    if ($ok -and [version]$version -ge [version]'3.10') { $py = @($cmd.Source) + $extra; break }
}
if (-not $py) { throw 'Python 3.10 or newer is required. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.' }

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    Write-Host 'Creating the environment...'
    & $py[0] @($py | Select-Object -Skip 1) -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

Write-Host 'Installing dependencies...'
& $venvPython -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

if (-not $SkipModel) {
    & $venvPython dictate.py --download
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed.' }
}

# Start Menu shortcut (and optionally a Startup one) that runs without a console window.
$shell = New-Object -ComObject WScript.Shell
function New-DictateShortcut($folder) {
    $link = $shell.CreateShortcut((Join-Path $folder 'Dictate.lnk'))
    $link.TargetPath = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    $link.Arguments = '"{0}"' -f (Join-Path $PSScriptRoot 'dictate.py')
    $link.WorkingDirectory = $PSScriptRoot
    $link.Description = 'Dictate: press Ctrl+Win to talk'
    $link.Save()
    Write-Host "Shortcut: $folder\Dictate.lnk"
}
New-DictateShortcut ([Environment]::GetFolderPath('Programs'))
if ($Startup) { New-DictateShortcut ([Environment]::GetFolderPath('Startup')) }

Write-Host ''
Write-Host 'Done. Open "Dictate" from the Start menu (a microphone appears in the tray),'
Write-Host 'then press Ctrl+Win to start dictating and Ctrl+Win again to stop.'

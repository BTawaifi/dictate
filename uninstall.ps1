<#
.SYNOPSIS
  Removes the Dictate shortcuts created by install.ps1 (only ones that point at this folder).
  Add -DeleteModel to also delete the downloaded model. The project folder (including
  .venv) can then simply be deleted.
#>
[CmdletBinding()]
param([switch]$DeleteModel)

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Startup'))) {
    $path = Join-Path $folder 'Dictate.lnk'
    if (-not (Test-Path -LiteralPath $path)) { continue }
    if ($shell.CreateShortcut($path).Arguments -like "*$PSScriptRoot*") {
        Remove-Item -LiteralPath $path
        Write-Host "Removed $path"
    } else {
        Write-Warning "Skipped $path (not created by this copy)"
    }
}
if ($DeleteModel) {
    $models = Join-Path $env:LOCALAPPDATA 'dictate'
    if (Test-Path -LiteralPath $models) { Remove-Item -LiteralPath $models -Recurse -Force; Write-Host "Removed $models" }
}

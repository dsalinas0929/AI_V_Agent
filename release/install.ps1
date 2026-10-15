$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Get-Content (Join-Path $PSScriptRoot 'SHA256SUMS') | ForEach-Object {
    $expected, $file = $_ -split '  ', 2
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $PSScriptRoot $file)).Hash
    if ($actual.ToLowerInvariant() -ne $expected) { throw "Checksum mismatch: $file" }
}

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($UvCommand) {
    $Uv = $UvCommand.Source
} else {
    $Uv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $Uv)) {
        Write-Host 'Installing uv from astral.sh for your user account...'
        $installer = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName() + '.ps1')
        try {
            Invoke-WebRequest -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installer
            & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
            if ($LASTEXITCODE -ne 0) { throw 'uv installation failed.' }
        } finally {
            Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue
        }
    }
}

$Wheels = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter 'agent_v-*.whl')
if ($Wheels.Count -ne 1) { throw 'Extract the release ZIP into an empty folder; expected one wheel.' }
& $Uv tool install --reinstall --python 3.13 $Wheels[0].FullName
if ($LASTEXITCODE -ne 0) { throw 'Agent V installation failed.' }
& $Uv tool update-shell
if ($LASTEXITCODE -ne 0) { throw 'Could not update PATH. See INSTALL.md.' }
$BinDir = & $Uv tool dir --bin
if ($LASTEXITCODE -ne 0) { throw 'Could not find installed commands.' }
& (Join-Path $BinDir 'agent-v.exe') --version
if ($LASTEXITCODE -ne 0) { throw 'Agent V startup failed.' }
Write-Host "`nInstalled. Open a NEW terminal, then run: agent-v setup"

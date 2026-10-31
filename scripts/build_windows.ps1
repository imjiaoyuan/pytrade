param(
    [string]$EnvironmentPath = ".venv-windows",
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "This build requires Windows x64." }
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$BuildStamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BuildRoot = Join-Path $RepoRoot "build\windows\$BuildStamp"
New-Item -ItemType Directory -Path $BuildRoot -Force | Out-Null
$PythonPath = Join-Path $EnvironmentPath "Scripts\python.exe"

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Executable" }
}

if (-not (Test-Path $PythonPath)) {
    Invoke-Checked -Executable "py" -Arguments @("-3.12", "-m", "venv", $EnvironmentPath)
}
$PythonPath = (Resolve-Path $PythonPath).Path
Invoke-Checked -Executable $PythonPath -Arguments @("-c", "import struct,sys; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8, 'Python 3.12 x64 required'")
Invoke-Checked -Executable $PythonPath -Arguments @("-m", "pip", "install", "-r", "requirements-windows.lock")
$env:PYTHONPATH = Join-Path $RepoRoot "src"
Invoke-Checked -Executable $PythonPath -Arguments @("-m", "unittest", "discover", "-s", "tests")
Invoke-Checked -Executable $PythonPath -Arguments @("src/main.py", "--diagnostics", (Join-Path $BuildRoot "source-diagnostics.json"), "--native-check")
$Arguments = @(
    "-m", "nuitka", "--standalone", "--enable-plugin=pyside6", "--msvc=latest",
    "--include-package=openctp_ctp", "--windows-console-mode=disable", "--output-filename=PyTrade.exe",
    "--company-name=PyTrade", "--product-name=PyTrade Preview", "--windows-file-version=0.1.0.1", "--windows-product-version=0.1.0.1",
    "--include-data-files=data/service_catalog.json=data/service_catalog.json",
    "--include-data-files=data/Instruments.snapshot.xml=data/Instruments.snapshot.xml",
    "--include-data-files=data/original_dom_spec.json=data/original_dom_spec.json",
    "--include-data-dir=assets/sounds=assets/sounds",
    "--output-dir=$BuildRoot", "--report=$(Join-Path $BuildRoot 'nuitka-report.xml')", "src/main.py"
)
Invoke-Checked -Executable $PythonPath -Arguments $Arguments
$DistDirectories = @(Get-ChildItem -Path $BuildRoot -Directory -Filter "*.dist")
if ($DistDirectories.Count -ne 1) { throw "Expected one standalone distribution directory." }
$DistDirectory = $DistDirectories[0].FullName
Invoke-Checked -Executable $PythonPath -Arguments @("scripts/verify_windows_bundle.py", $DistDirectory, "--output", (Join-Path $BuildRoot "bundle-verification.json"))
Compress-Archive -Path $DistDirectory -DestinationPath (Join-Path $BuildRoot "PyTrade-0.1.0rc1-windows-x64.zip")
if ($Installer) {
    Invoke-Checked -Executable "ISCC.exe" -Arguments @("/DSourceDir=$DistDirectory", "/O$BuildRoot", "packaging/windows/PyTrade.iss")
}
Write-Output "Build and validation outputs: $BuildRoot"

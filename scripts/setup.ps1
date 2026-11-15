param(
    [string]$Python = '',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repo '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

function Get-CompatiblePython([string]$Executable, [string[]]$Prefix = @()) {
    try {
        $result = & $Executable @Prefix -I -c 'import sys; print(sys.executable) if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else sys.exit(1)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) { return ([string]($result | Select-Object -Last 1)).Trim() }
    } catch { }
    return $null
}

$existing = Test-Path -LiteralPath $venv
$selected = $null
if ($existing) {
    if (Test-Path -LiteralPath $venvPython) { $selected = Get-CompatiblePython $venvPython }
    if (-not $selected) { throw 'Existing .venv is not a working 64-bit Python 3.11 environment. It was left unchanged.' }
} elseif ($Python) {
    $selected = Get-CompatiblePython $Python
    if (-not $selected) { throw '-Python must point to a working 64-bit Python 3.11 executable.' }
} else {
    $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($launcher) { $selected = Get-CompatiblePython $launcher.Source @('-3.11') }
    if (-not $selected) {
        foreach ($name in @('python3.11', 'python', 'python3')) {
            foreach ($command in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
                # Avoid Windows Store aliases, which may open an installer instead of Python.
                if ($command.Source -match '\\Microsoft\\WindowsApps\\') { continue }
                $selected = Get-CompatiblePython $command.Source
                if ($selected) { break }
            }
            if ($selected) { break }
        }
    }
    if (-not $selected) {
        foreach ($key in @('HKCU:\Software\Python\PythonCore\*\InstallPath',
                           'HKLM:\Software\Python\PythonCore\*\InstallPath',
                           'HKLM:\Software\WOW6432Node\Python\PythonCore\*\InstallPath')) {
            foreach ($item in @(Get-Item -Path $key -ErrorAction SilentlyContinue)) {
                $executable = $item.GetValue('ExecutablePath')
                if (-not $executable) {
                    $installPath = $item.GetValue('')
                    if (-not $installPath) { continue }
                    $executable = Join-Path $installPath 'python.exe'
                }
                $selected = Get-CompatiblePython $executable
                if ($selected) { break }
            }
            if ($selected) { break }
        }
    }
    if (-not $selected) {
        throw '64-bit Python 3.11 was not found. Install Python 3.11 and rerun this command; a launcher is not required. Alternatively pass -Python "C:\path\to\python.exe".'
    }
}
if ($CheckOnly) {
    @{ python = $selected; version = '3.11'; existing_venv = $existing; changed = $false } | ConvertTo-Json -Compress
    exit 0
}
if ($existing) {
    & $venvPython -I -c 'import src, osmium, shapely, pyproj'
    if ($LASTEXITCODE -ne 0) { throw 'Existing .venv is incomplete; it was left unchanged. Install the project requirements before continuing.' }
    Write-Host "Existing environment is available: $venvPython (left unchanged)."
    exit 0
}

foreach ($relative in @('.local\tmp', '.local\pip-cache')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $repo $relative) | Out-Null
}
$env:TMP = Join-Path $repo '.local\tmp'
$env:TEMP = $env:TMP
$env:TMPDIR = $env:TMP
$env:PIP_CACHE_DIR = Join-Path $repo '.local\pip-cache'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $selected -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Creating .venv failed.' }
& $venvPython -m pip install -r (Join-Path $repo 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. The partial .venv was retained; see the pip error above.' }
& $venvPython -m pip install --no-deps --no-build-isolation -e $repo
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed; see the pip error above.' }
& $venvPython -B (Join-Path $repo 'main.py') --help
if ($LASTEXITCODE -ne 0) { throw 'CLI verification failed.' }
Write-Host 'Environment setup complete. Continue with local configs, PBF and candidates in README Setup.'

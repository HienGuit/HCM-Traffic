param(
    [ValidateSet('collect','transform','dry-run')][string]$Action='dry-run',
    [string]$Date='',
    [string]$Runtime=(Join-Path $PSScriptRoot '..\config\local\runtime.json')
)
$ErrorActionPreference='Stop'
$r=Get-Content -LiteralPath $Runtime -Raw | ConvertFrom-Json
$runtimeRoot=Split-Path -Parent (Resolve-Path -LiteralPath $Runtime).Path
foreach($field in @('python','config','plan')){
    if(-not [IO.Path]::IsPathRooted($r.$field)){$r.$field=[IO.Path]::GetFullPath((Join-Path $runtimeRoot $r.$field))}
}
$python=$r.python
$repoRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONIOENCODING='utf-8'
if(-not $Date){$Date=& $python -c "from datetime import datetime; from zoneinfo import ZoneInfo; print(datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).date())"}
if($Action -eq 'collect'){
    if(-not $env:TOMTOM_API_KEY){
        $secure=Read-Host 'TomTom API key (process only)' -AsSecureString
        $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try{$env:TOMTOM_API_KEY=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)}
        finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)}
    }
    & $python -B (Join-Path $repoRoot 'main.py') run --config $r.config --date $Date --allow-online
}elseif($Action -eq 'transform'){
    & $python -B (Join-Path $repoRoot 'main.py') transform --config $r.config --date $Date
}else{
    & $python -B (Join-Path $repoRoot 'main.py') --config $r.config capture dry-run --plan $r.plan --date $Date --allow-online
}
exit $LASTEXITCODE

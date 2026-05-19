param(
    [Parameter(Mandatory = $true)]
    [string]$Project
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if ([System.IO.Path]::IsPathRooted($Project)) {
    $ProjectPath = $Project
} else {
    $ProjectPath = Join-Path $Root $Project
}
$Preflight = Join-Path $Root "scripts/cli/preflight_check.py"

python $Preflight --project $ProjectPath

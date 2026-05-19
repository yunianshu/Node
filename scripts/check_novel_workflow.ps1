param(
    [string]$Project = "projects/novels6"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if ([System.IO.Path]::IsPathRooted($Project)) {
    $ProjectPath = $Project
} else {
    $ProjectPath = Join-Path $Root $Project
}
$Preflight = Join-Path $Root "novel-tools/preflight_check.py"

python $Preflight --project $ProjectPath

# Baut ATAS-Erweiterungen ohne Visual Studio / .NET-SDK:
# alte csc.exe (C# 5) + Referenzen direkt auf ATAS-DLLs und die installierte .NET-Runtime.
# Code muss daher C#-5-kompatibel sein (keine $"..."-Strings, kein "=>" bei Membern, kein "?.").
#
#   .\build.ps1 -Project Exporter           # baut OrderFlowExporter.dll nach bin\
#   .\build.ps1 -Project Exporter -Install  # ... und kopiert sie in den ATAS-Indicators-Ordner
#   .\build.ps1 -Project Bot                # baut OrderFlowAutoBot.dll
param(
    [ValidateSet("Bot", "Exporter")] [string] $Project = "Exporter",
    [switch] $Install
)
$ErrorActionPreference = "Stop"

$atas = "C:\Program Files (x86)\ATAS Platform"
$rt = Get-ChildItem "C:\Program Files\dotnet\shared\Microsoft.NETCore.App" -Directory |
      Sort-Object { [version]$_.Name } | Select-Object -Last 1 -ExpandProperty FullName
$csc = "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"

$runtimeRefs = "System.Runtime", "System.Private.CoreLib", "System.Collections", "System.ObjectModel",
               "System.ComponentModel", "System.ComponentModel.Primitives", "System.ComponentModel.TypeConverter",
               "System.ComponentModel.Annotations", "System.Drawing.Primitives", "System.Linq",
               "System.Runtime.Extensions", "System.IO", "System.Text.Encoding.Extensions", "netstandard"
$atasRefs = "ATAS.Indicators", "ATAS.Indicators.Technical", "ATAS.Strategies", "ATAS.DataFeedsCore",
            "ATAS.Types", "Utils.Common"

$refs = @()
foreach ($n in $runtimeRefs) { $p = Join-Path $rt "$n.dll";   if (Test-Path $p) { $refs += "/r:$p" } }
foreach ($n in $atasRefs)    { $p = Join-Path $atas "$n.dll"; if (Test-Path $p) { $refs += "/r:$p" } }

if ($Project -eq "Bot") {
    $sources = Get-ChildItem $PSScriptRoot -Filter "OrderFlowSignalStrategy*.cs" | ForEach-Object FullName
    $name = "OrderFlowAutoBot"
} else {
    $sources = @(Join-Path $PSScriptRoot "Exporter\OrderFlowExporter.cs")
    $name = "OrderFlowExporter"
}

$outDir = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force $outDir | Out-Null
$out = Join-Path $outDir "$name.dll"

& $csc /nologo /noconfig /nostdlib+ /optimize+ /target:library "/out:$out" $refs $sources
if ($LASTEXITCODE -ne 0) { throw "Build fehlgeschlagen ($name)" }
Write-Host "OK: $out"

if ($Install) {
    $dest = Join-Path $env:APPDATA "ATAS\Indicators"
    Copy-Item $out $dest -Force
    Write-Host "Installiert nach $dest - in ATAS Indikator-Liste neu laden."
}

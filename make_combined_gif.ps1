param(
    [string]$InputDir = "E:\ZBN\dataset-build-video\dataset\0",
    [string]$IndexSpec = "0-7",
    [switch]$UseAllAvi,
    [string]$OutputPath,
    [string]$OutputMp4Path,
    [int]$NormalizeFps = 12,
    [double]$Speed,
    [double]$MaxGifMB = 9.5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Require-Command {
    param([string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Format-SizeMB {
    param([long]$Bytes)

    return "{0:N2}" -f ($Bytes / 1MB)
}

function Parse-IndexSpec {
    param([string]$Spec)

    $indices = New-Object System.Collections.Generic.List[int]

    foreach ($part in ($Spec -split ",")) {
        $trimmed = $part.Trim()
        if (-not $trimmed) {
            continue
        }

        if ($trimmed -match "^(\d+)-(\d+)$") {
            $start = [int]$matches[1]
            $end = [int]$matches[2]

            if ($start -gt $end) {
                throw "Invalid range in IndexSpec: $trimmed"
            }

            foreach ($value in $start..$end) {
                $indices.Add($value)
            }
            continue
        }

        if ($trimmed -match "^\d+$") {
            $indices.Add([int]$trimmed)
            continue
        }

        throw "Invalid token in IndexSpec: $trimmed"
    }

    if ($indices.Count -eq 0) {
        throw "IndexSpec resolved to an empty set."
    }

    return $indices.ToArray()
}

function Get-Presets {
    param([double]$RequestedSpeed)

    if ($RequestedSpeed -gt 0) {
        return @(
            @{ Width = 320; Fps = 10; Speed = $RequestedSpeed },
            @{ Width = 280; Fps = 8;  Speed = $RequestedSpeed },
            @{ Width = 240; Fps = 8;  Speed = $RequestedSpeed },
            @{ Width = 220; Fps = 7;  Speed = $RequestedSpeed },
            @{ Width = 200; Fps = 6;  Speed = $RequestedSpeed },
            @{ Width = 180; Fps = 5;  Speed = $RequestedSpeed }
        )
    }

    return @(
        @{ Width = 360; Fps = 10; Speed = 2.0 },
        @{ Width = 320; Fps = 10; Speed = 2.0 },
        @{ Width = 320; Fps = 8;  Speed = 2.0 },
        @{ Width = 280; Fps = 8;  Speed = 2.5 },
        @{ Width = 240; Fps = 8;  Speed = 3.0 },
        @{ Width = 220; Fps = 7;  Speed = 3.5 },
        @{ Width = 200; Fps = 6;  Speed = 4.0 },
        @{ Width = 180; Fps = 5;  Speed = 4.5 }
    )
}

function Test-ValidMediaFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }

    & ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1:nokey=0 $Path | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Invoke-CheckedFfmpeg {
    param(
        [string[]]$Arguments,
        [string]$ExpectedOutput
    )

    & ffmpeg @Arguments
    if ($LASTEXITCODE -ne 0) {
        if ($ExpectedOutput -and (Test-ValidMediaFile -Path $ExpectedOutput)) {
            return
        }
        throw "ffmpeg failed with exit code $LASTEXITCODE"
    }
}

Require-Command -Name "ffmpeg"
Require-Command -Name "ffprobe"

$resolvedInputDir = (Resolve-Path -LiteralPath $InputDir).Path

if ($UseAllAvi) {
    $aviFiles = Get-ChildItem -LiteralPath $resolvedInputDir -Filter "*.avi" | Sort-Object `
        @{ Expression = {
                $stem = [System.IO.Path]::GetFileNameWithoutExtension($_.Name)
                if ($stem -match '^\d+$') { [int]$stem } else { [int]::MaxValue }
            }
        }, `
        @{ Expression = { $_.Name } }

    if (-not $aviFiles) {
        throw "No AVI files found in $resolvedInputDir"
    }

    $videoPaths = $aviFiles | ForEach-Object { $_.FullName }
    $indicesForName = $aviFiles | ForEach-Object { [System.IO.Path]::GetFileNameWithoutExtension($_.Name) }
}
else {
    $Indices = Parse-IndexSpec -Spec $IndexSpec
    $videoPaths = foreach ($index in $Indices) {
        $candidate = Join-Path $resolvedInputDir ("{0}.avi" -f $index)
        if (-not (Test-Path -LiteralPath $candidate)) {
            throw "Video not found: $candidate"
        }
        (Resolve-Path -LiteralPath $candidate).Path
    }
    $indicesForName = $Indices | ForEach-Object { $_.ToString() }
}

if (-not $OutputPath) {
    $joined = $indicesForName -join "_"
    $OutputPath = Join-Path $resolvedInputDir "combined_${joined}.gif"
}

$resolvedGifPath = [System.IO.Path]::GetFullPath($OutputPath)
if (-not $OutputMp4Path) {
    $OutputMp4Path = [System.IO.Path]::ChangeExtension($resolvedGifPath, ".mp4")
}
$resolvedMp4Path = [System.IO.Path]::GetFullPath($OutputMp4Path)

$outputDir = Split-Path -Parent $OutputPath
if ($outputDir -and -not (Test-Path -LiteralPath $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir | Out-Null
}

$mp4OutputDir = Split-Path -Parent $resolvedMp4Path
if ($mp4OutputDir -and -not (Test-Path -LiteralPath $mp4OutputDir)) {
    New-Item -ItemType Directory -Path $mp4OutputDir | Out-Null
}

$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("combined_gif_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempDir | Out-Null

try {
    $normalizedDir = Join-Path $tempDir "normalized"
    New-Item -ItemType Directory -Path $normalizedDir | Out-Null

    $normalizedPaths = @()
    for ($i = 0; $i -lt $videoPaths.Count; $i++) {
        $videoPath = $videoPaths[$i]
        $normalizedPath = Join-Path $normalizedDir ("clip_{0:D3}.mkv" -f $i)
        $normalizeFilter = "fps=$NormalizeFps,format=yuv420p"

        Invoke-CheckedFfmpeg -Arguments @(
            '-y', '-v', 'warning',
            '-threads', '1',
            '-fflags', '+genpts',
            '-err_detect', 'ignore_err',
            '-i', $videoPath,
            '-an',
            '-vf', $normalizeFilter,
            '-r', $NormalizeFps,
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',
            '-avoid_negative_ts', 'make_zero',
            $normalizedPath
        ) -ExpectedOutput $normalizedPath

        if (-not (Test-Path -LiteralPath $normalizedPath)) {
            throw "Failed to normalize clip: $videoPath"
        }

        $normalizedPaths += $normalizedPath
    }

    $concatFile = Join-Path $tempDir "normalized_inputs.txt"
    $concatLines = foreach ($normalizedPath in $normalizedPaths) {
        $ffmpegPath = $normalizedPath.Replace("\", "/").Replace("'", "''")
        "file '$ffmpegPath'"
    }
    [System.IO.File]::WriteAllLines($concatFile, $concatLines, [System.Text.Encoding]::ASCII)

    $combinedMp4 = Join-Path $tempDir "combined.mp4"
    Invoke-CheckedFfmpeg -Arguments @(
        '-y', '-v', 'warning',
        '-threads', '1',
        '-fflags', '+genpts',
        '-err_detect', 'ignore_err',
        '-f', 'concat',
        '-safe', '0',
        '-i', $concatFile,
        '-an',
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-crf', '23',
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-avoid_negative_ts', 'make_zero',
        $combinedMp4
    ) -ExpectedOutput $combinedMp4

    if (-not (Test-Path -LiteralPath $combinedMp4)) {
        throw "Failed to create combined MP4"
    }

    Copy-Item -LiteralPath $combinedMp4 -Destination $resolvedMp4Path -Force

    $presets = Get-Presets -RequestedSpeed $Speed

    $selected = $null
    $attempts = @()

    foreach ($preset in $presets) {
        $speedFactor = "{0:0.###}" -f (1.0 / [double]$preset.Speed)
        $label = "{0}w_{1}fps_{2}x" -f $preset.Width, $preset.Fps, (($preset.Speed).ToString().Replace(".", "_"))
        $candidateGif = Join-Path $tempDir ("$label.gif")
        $filter = "setpts=${speedFactor}*PTS,fps=$($preset.Fps),scale=$($preset.Width):-1:flags=lanczos,split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3"

        Invoke-CheckedFfmpeg -Arguments @(
            '-y', '-v', 'warning',
            '-threads', '1',
            '-err_detect', 'ignore_err',
            '-i', $combinedMp4,
            '-filter_complex', $filter,
            $candidateGif
        ) -ExpectedOutput $candidateGif

        if (-not (Test-Path -LiteralPath $candidateGif)) {
            throw "Failed to create GIF with preset $label"
        }

        $sizeBytes = (Get-Item -LiteralPath $candidateGif).Length
        $result = [pscustomobject]@{
            Path = $candidateGif
            Width = $preset.Width
            Fps = $preset.Fps
            Speed = $preset.Speed
            SizeBytes = $sizeBytes
        }
        $attempts += $result

        if (($sizeBytes / 1MB) -le $MaxGifMB) {
            $selected = $result
            break
        }
    }

    if (-not $selected) {
        $selected = $attempts[-1]
        Write-Warning ("No preset reached the target size of {0:N2} MB. Using the smallest preset result instead ({1:N2} MB)." -f $MaxGifMB, ($selected.SizeBytes / 1MB))
    }

    Copy-Item -LiteralPath $selected.Path -Destination $resolvedGifPath -Force

    $summary = [pscustomobject]@{
        output_gif = $resolvedGifPath
        output_mp4 = $resolvedMp4Path
        size_mb = [math]::Round(($selected.SizeBytes / 1MB), 2)
        normalize_fps = $NormalizeFps
        width = $selected.Width
        fps = $selected.Fps
        speed = $selected.Speed
        files = ($videoPaths | ForEach-Object { Split-Path -Leaf $_ }) -join ", "
    }

    $summary | Format-List
}
finally {
    if (Test-Path -LiteralPath $tempDir) {
        Remove-Item -LiteralPath $tempDir -Recurse -Force
    }
}

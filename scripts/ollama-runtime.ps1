param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "status", "stop", "pull", "models", "verify")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:RuntimeRoot = Join-Path $script:RepoRoot ".runtime\ollama"
$script:Executable = Join-Path $script:RuntimeRoot "ollama.exe"
$script:ModelsDirectory = Join-Path $script:RuntimeRoot "models"
$script:StatePath = Join-Path $script:RuntimeRoot "server-owner.json"
$script:Endpoint = "http://127.0.0.1:11435"
$script:OllamaHost = "127.0.0.1:11435"
$script:ModelName = "qwen3.5:9b-q4_K_M"
$script:Port = 11435

function Read-OwnerState {
    if (-not (Test-Path -LiteralPath $script:StatePath -PathType Leaf)) {
        return $null
    }

    $state = Get-Content -LiteralPath $script:StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $state.pid -or -not $state.executable -or -not $state.startedUtc) {
        throw "The runtime owner record is invalid. No process was changed."
    }
    return $state
}

function Get-RecordedProcess {
    param([Parameter(Mandatory = $true)]$State)

    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int]$State.pid)
    }
    catch {
        if ($_.Exception -is [System.ArgumentException] -or $_.Exception.InnerException -is [System.ArgumentException]) {
            return $null
        }
        throw
    }

    if ($process.HasExited) {
        return $null
    }

    try {
        $actualPath = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
    }
    catch {
        throw "Cannot verify the recorded process image. No process was changed."
    }

    $expectedPath = [System.IO.Path]::GetFullPath($script:Executable)
    if (-not [string]::Equals($actualPath, $expectedPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The recorded PID now points to a different executable. No process was changed."
    }

    $expectedStart = [DateTime]::Parse([string]$State.startedUtc).ToUniversalTime().Ticks
    $actualStart = $process.StartTime.ToUniversalTime().Ticks
    if ($expectedStart -ne $actualStart) {
        throw "The recorded PID has been reused by another process. No process was changed."
    }

    return $process
}

function Test-PortAvailable {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Parse("127.0.0.1"),
        [int]$script:Port
    )
    try {
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

function Get-EndpointVersion {
    $request = $null
    $response = $null
    $reader = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("$($script:Endpoint)/api/version")
        $request.Proxy = $null
        $request.Timeout = 2000
        $response = $request.GetResponse()
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
        return [string]$payload.version
    }
    catch {
        return $null
    }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

function Write-OwnerState {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$StdoutLog,
        [Parameter(Mandatory = $true)][string]$StderrLog
    )

    $Process.Refresh()
    $state = [ordered]@{
        pid = $Process.Id
        executable = [System.IO.Path]::GetFullPath($script:Executable)
        startedUtc = $Process.StartTime.ToUniversalTime().ToString("o")
        endpoint = $script:Endpoint
        modelsDirectory = [System.IO.Path]::GetFullPath($script:ModelsDirectory)
        stdoutLog = $StdoutLog
        stderrLog = $StderrLog
    }

    $temporaryPath = "$($script:StatePath).$([Guid]::NewGuid().ToString('N')).tmp"
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $script:StatePath -Force
    return $state
}

function Start-ManagedRuntime {
    if (-not (Test-Path -LiteralPath $script:Executable -PathType Leaf)) {
        throw "Project runtime is missing: $($script:Executable)"
    }

    $state = Read-OwnerState
    if ($state) {
        $existing = Get-RecordedProcess -State $state
        if ($existing) {
            $version = Get-EndpointVersion
            if ($version) {
                Write-Output "running=true endpoint=$($script:Endpoint) version=$version owner_pid=$($existing.Id)"
                return
            }
            throw "The project-owned server process exists but is not healthy. Inspect its runtime logs; no duplicate was started."
        }
    }

    if (-not (Test-PortAvailable)) {
        throw "Port $($script:Port) is occupied by an unowned process. It was not contacted or stopped."
    }

    New-Item -ItemType Directory -Path $script:ModelsDirectory -Force | Out-Null
    $logDirectory = Join-Path $script:RuntimeRoot "logs"
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $stdoutLog = Join-Path $logDirectory "server-$stamp.stdout.log"
    $stderrLog = Join-Path $logDirectory "server-$stamp.stderr.log"

    $hadHost = Test-Path Env:OLLAMA_HOST
    $oldHost = $env:OLLAMA_HOST
    $hadModels = Test-Path Env:OLLAMA_MODELS
    $oldModels = $env:OLLAMA_MODELS
    $hadNoCloud = Test-Path Env:OLLAMA_NO_CLOUD
    $oldNoCloud = $env:OLLAMA_NO_CLOUD
    try {
        $env:OLLAMA_HOST = $script:OllamaHost
        $env:OLLAMA_MODELS = [System.IO.Path]::GetFullPath($script:ModelsDirectory)
        $env:OLLAMA_NO_CLOUD = "1"
        $process = Start-Process `
            -FilePath $script:Executable `
            -ArgumentList "serve" `
            -WorkingDirectory $script:RuntimeRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru
    }
    finally {
        if ($hadHost) { $env:OLLAMA_HOST = $oldHost } else { Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue }
        if ($hadModels) { $env:OLLAMA_MODELS = $oldModels } else { Remove-Item Env:OLLAMA_MODELS -ErrorAction SilentlyContinue }
        if ($hadNoCloud) { $env:OLLAMA_NO_CLOUD = $oldNoCloud } else { Remove-Item Env:OLLAMA_NO_CLOUD -ErrorAction SilentlyContinue }
    }

    $state = Write-OwnerState -Process $process -StdoutLog $stdoutLog -StderrLog $stderrLog
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        $version = Get-EndpointVersion
        if ($version) {
            Write-Output "running=true endpoint=$($script:Endpoint) version=$version owner_pid=$($process.Id)"
            while ($true) {
                $process.Refresh()
                if ($process.HasExited) {
                    Write-Output "running=false owner_pid=$($process.Id) exit_code=$($process.ExitCode)"
                    return
                }
                $process.WaitForExit(1000) | Out-Null
            }
        }

        $process.Refresh()
        if ($process.HasExited) {
            throw "The project-owned Ollama process exited during startup. Inspect $stderrLog"
        }
        Start-Sleep -Seconds 1
    }

    throw "The project-owned Ollama process did not become healthy within 60 seconds. Inspect $stderrLog"
}

function Get-HealthyManagedRuntime {
    $state = Read-OwnerState
    if (-not $state) {
        throw "No project-owned Ollama server record exists. Start it with scripts\ollama-runtime.ps1 start."
    }

    $process = Get-RecordedProcess -State $state
    if (-not $process) {
        throw "The recorded project process is no longer running. State was preserved; no process was changed."
    }

    $version = Get-EndpointVersion
    if (-not $version) {
        throw "The recorded project process does not answer at $($script:Endpoint). No process was changed."
    }

    return @{ process = $process; state = $state; version = $version }
}

function Stop-ManagedRuntime {
    $state = Read-OwnerState
    if (-not $state) {
        if (Test-PortAvailable) {
            Write-Output "running=false managed_process=none"
            return
        }
        throw "Port $($script:Port) is occupied but has no project owner record. It was not stopped."
    }

    $process = Get-RecordedProcess -State $state
    if (-not $process) {
        if (-not (Test-PortAvailable)) {
            throw "The recorded process is not running and port $($script:Port) is occupied. Owner state was preserved; no process was changed."
        }

        Remove-Item -LiteralPath $script:StatePath
        Write-Output "running=false stale_owner_record_cleared=true"
        return
    }

    $process.Kill()
    if (-not $process.WaitForExit(10000)) {
        throw "The verified project-owned process did not stop within 10 seconds. Owner state was preserved."
    }

    Remove-Item -LiteralPath $script:StatePath
    Write-Output "running=false stopped_owner_pid=$($process.Id)"
}

function Invoke-WithRuntimeEnvironment {
    param([Parameter(Mandatory = $true)][scriptblock]$Command)

    $hadHost = Test-Path Env:OLLAMA_HOST
    $oldHost = $env:OLLAMA_HOST
    $hadModels = Test-Path Env:OLLAMA_MODELS
    $oldModels = $env:OLLAMA_MODELS
    $hadNoCloud = Test-Path Env:OLLAMA_NO_CLOUD
    $oldNoCloud = $env:OLLAMA_NO_CLOUD
    try {
        $env:OLLAMA_HOST = $script:OllamaHost
        $env:OLLAMA_MODELS = [System.IO.Path]::GetFullPath($script:ModelsDirectory)
        $env:OLLAMA_NO_CLOUD = "1"
        & $Command
        return $LASTEXITCODE
    }
    finally {
        if ($hadHost) { $env:OLLAMA_HOST = $oldHost } else { Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue }
        if ($hadModels) { $env:OLLAMA_MODELS = $oldModels } else { Remove-Item Env:OLLAMA_MODELS -ErrorAction SilentlyContinue }
        if ($hadNoCloud) { $env:OLLAMA_NO_CLOUD = $oldNoCloud } else { Remove-Item Env:OLLAMA_NO_CLOUD -ErrorAction SilentlyContinue }
    }
}

function Invoke-ModelPull {
    $null = Get-HealthyManagedRuntime
    Write-Output "pulling model=$($script:ModelName) endpoint=$($script:Endpoint)"
    $code = Invoke-WithRuntimeEnvironment { & $script:Executable pull $script:ModelName }
    if ($code -ne 0) {
        throw "Ollama model pull failed with exit code $code. The project-owned server remains running."
    }
}

function Show-Models {
    $null = Get-HealthyManagedRuntime
    $code = Invoke-WithRuntimeEnvironment { & $script:Executable list }
    if ($code -ne 0) {
        throw "Ollama model listing failed with exit code $code."
    }
}

function Invoke-SyntheticVerification {
    $null = Get-HealthyManagedRuntime
    $python = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python was not found. Create the project environment using its README instructions."
        }
        $python = $pythonCommand.Source
    }

    & $python -m ssr_chat verify-ollama `
        --ollama-endpoint $script:Endpoint `
        --ollama-model $script:ModelName
    if ($LASTEXITCODE -ne 0) {
        throw "Synthetic Ollama verification failed with exit code $LASTEXITCODE."
    }
}

try {
    switch ($Action) {
        "start" { Start-ManagedRuntime }
        "status" {
            $state = Read-OwnerState
            if (-not $state) {
                if (Test-PortAvailable) {
                    Write-Output "running=false managed_process=none"
                    break
                }
                throw "Port $($script:Port) is occupied by an unowned process. It was not contacted or stopped."
            }
            $process = Get-RecordedProcess -State $state
            if (-not $process) {
                throw "The recorded project process is not running. Owner state was preserved."
            }
            $version = Get-EndpointVersion
            if (-not $version) {
                throw "The recorded process is not healthy at $($script:Endpoint)."
            }
            Write-Output "running=true endpoint=$($script:Endpoint) version=$version owner_pid=$($process.Id) models=$($script:ModelsDirectory)"
        }
        "stop" { Stop-ManagedRuntime }
        "pull" { Invoke-ModelPull }
        "models" { Show-Models }
        "verify" { Invoke-SyntheticVerification }
    }
}
catch {
    [Console]::Error.WriteLine("ollama-runtime: $($_.Exception.Message)")
    throw
}

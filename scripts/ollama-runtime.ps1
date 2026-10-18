param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "status", "stop", "pull", "models", "verify", "run-9b")]
    [string]$Action = "status",
    [switch]$ReturnAfterHealthy
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
Import-Module (Join-Path $PSScriptRoot "ollama-runtime-lifecycle.psm1") -Force

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

function Get-OwnerProcessObservation {
    param([Parameter(Mandatory = $true)]$State)

    try {
        $process = [System.Diagnostics.Process]::GetProcessById([int]$State.pid)
    }
    catch {
        if ($_.Exception -is [System.ArgumentException] -or $_.Exception.InnerException -is [System.ArgumentException]) {
            return [pscustomobject]@{ status = "absent"; process = $null; identity = $null }
        }
        return [pscustomobject]@{ status = "unknown"; process = $null; identity = $null }
    }

    if ($process.HasExited) {
        return [pscustomobject]@{ status = "absent"; process = $null; identity = $null }
    }

    try {
        $identity = [ordered]@{
            pid = $process.Id
            executable = [System.IO.Path]::GetFullPath($process.MainModule.FileName)
            startedUtc = $process.StartTime.ToUniversalTime().ToString("o")
        }
    }
    catch {
        return [pscustomobject]@{ status = "unknown"; process = $null; identity = $null }
    }

    if (Test-OllamaProcessIdentityMatch -Expected $State -Actual $identity) {
        return [pscustomobject]@{ status = "owned"; process = $process; identity = $identity }
    }

    return [pscustomobject]@{ status = "reused"; process = $process; identity = $identity }
}

function Get-RecordedProcess {
    param([Parameter(Mandatory = $true)]$State)

    $observation = Get-OwnerProcessObservation -State $State
    switch ($observation.status) {
        "owned" { return $observation.process }
        "absent" { return $null }
        "reused" { throw "The recorded PID has been reused by another process. No process was changed." }
        default { throw "Cannot verify the recorded process identity. No process was changed." }
    }
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

function Test-PortHasListener {
    try {
        $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    }
    catch {
        throw "Could not determine whether port $($script:Port) has a listener. Owner state was preserved."
    }

    return (@($listeners | Where-Object { $_.Port -eq [int]$script:Port }).Count -gt 0)
}

function Get-EndpointVersion {
    $client = [System.Net.Sockets.TcpClient]::new([System.Net.Sockets.AddressFamily]::InterNetwork)
    $reader = $null
    try {
        $connect = $client.ConnectAsync([System.Net.IPAddress]::Parse("127.0.0.1"), [int]$script:Port)
        if (-not $connect.Wait(2000)) {
            throw [System.TimeoutException]::new("Loopback connection timed out.")
        }
        [void]$connect.GetAwaiter().GetResult()

        $stream = $client.GetStream()
        $stream.ReadTimeout = 2000
        $stream.WriteTimeout = 2000
        $request = "GET /api/version HTTP/1.1`r`nHost: 127.0.0.1:$($script:Port)`r`nConnection: close`r`n`r`n"
        $requestBytes = [System.Text.Encoding]::ASCII.GetBytes($request)
        $stream.Write($requestBytes, 0, $requestBytes.Length)
        $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::UTF8, $false, 1024, $true)
        $responseText = $reader.ReadToEnd()
        $separator = $responseText.IndexOf("`r`n`r`n")
        if ($separator -lt 0) {
            throw [System.IO.InvalidDataException]::new("Loopback health response has no HTTP body separator.")
        }
        $lineEnd = $responseText.IndexOf("`r`n")
        $statusLine = $responseText.Substring(0, $lineEnd)
        if ($statusLine -notmatch '^HTTP/1\.[01] 200 ') {
            throw [System.IO.InvalidDataException]::new("Loopback health response was not HTTP 200.")
        }
        $payload = $responseText.Substring($separator + 4) | ConvertFrom-Json
        return [string]$payload.version
    }
    catch {
        $failure = $_.Exception
        $detail = "{0}: {1}" -f $failure.GetType().Name, $failure.Message
        if ($failure.InnerException) {
            $detail += " | {0}: {1}" -f $failure.InnerException.GetType().Name, $failure.InnerException.Message
        }
        $script:LastEndpointError = $detail
        return $null
    }
    finally {
        if ($reader) { $reader.Dispose() }
        $client.Dispose()
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

function Get-ProcessIdentityFromHandle {
    param([Parameter(Mandatory = $true)]$Process)

    $Process.Refresh()
    if ($Process.HasExited) { return $null }

    try {
        return [ordered]@{
            pid = $Process.Id
            executable = [System.IO.Path]::GetFullPath($Process.MainModule.FileName)
            startedUtc = $Process.StartTime.ToUniversalTime().ToString("o")
        }
    }
    catch {
        throw "Cannot verify the held Process object identity. No process was changed."
    }
}

function Resolve-OwnerBeforeStart {
    $state = Read-OwnerState
    if (-not $state) {
        return @{ status = "none"; process = $null }
    }

    $observation = Get-OwnerProcessObservation -State $state
    if ($observation.status -eq "owned") {
        return @{ status = "owned"; process = $observation.process; state = $state }
    }
    if ($observation.status -notin @("absent", "reused")) {
        throw "Could not prove that the recorded project process is gone. Owner state was preserved."
    }

    $hasListener = Test-PortHasListener
    $archivePath = Move-StaleOllamaOwnerRecordIfSafe `
        -OwnerPath $script:StatePath `
        -ExpectedState $state `
        -ProcessStatus $observation.status `
        -HasListener $hasListener
    [Console]::WriteLine("stale_owner_record_archived=$archivePath")
    return @{ status = "stale"; process = $null; archivePath = $archivePath }
}

function Start-ManagedRuntimeProcess {
    param([Parameter(Mandatory = $true)]$OwnerResolution)

    if (-not (Test-Path -LiteralPath $script:Executable -PathType Leaf)) {
        throw "Project runtime is missing: $($script:Executable)"
    }

    if ($OwnerResolution.status -eq "owned") {
        throw "A project-owned server is already running from another invocation. This workflow will not adopt or stop it."
    }
    if ($OwnerResolution.status -notin @("none", "stale")) {
        throw "Owner state is unresolved. No process was started."
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

    try {
        $identity = Get-ProcessIdentityFromHandle -Process $process
        if (-not $identity) { throw "The launched Ollama process exited before its identity could be recorded." }
        $null = Write-OwnerState -Process $process -StdoutLog $stdoutLog -StderrLog $stderrLog
    }
    catch {
        $process.Refresh()
        if (-not $process.HasExited) {
            $process.Kill()
            $null = $process.WaitForExit(10000)
        }
        throw
    }
    return $process
}

function Wait-ManagedRuntimeHealthy {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$Identity,
        [int]$TimeoutSeconds = 60
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            throw "The held project-owned Ollama process exited during startup. No other process was inspected or changed."
        }

        $currentIdentity = Get-ProcessIdentityFromHandle -Process $Process
        if (-not (Test-OllamaProcessIdentityMatch -Expected $Identity -Actual $currentIdentity)) {
            throw "The held project Process identity changed during startup. No process was changed."
        }

        $version = Get-EndpointVersion
        if ($version) { return $version }
        Start-Sleep -Seconds 1
    }

    throw "The held project-owned Ollama process did not become healthy within $TimeoutSeconds seconds. Health error: $script:LastEndpointError"
}

function Start-ManagedRuntime {
    param([switch]$ReturnAfterHealthy)

    $ownerResolution = Resolve-OwnerBeforeStart
    if ($ownerResolution.status -eq "owned") {
        $process = $ownerResolution.process
        $identity = Get-ProcessIdentityFromHandle -Process $process
        if (-not $identity) { throw "The project-owned server exited before health validation." }
    }
    else {
        $process = Start-ManagedRuntimeProcess -OwnerResolution $ownerResolution
        $identity = Get-ProcessIdentityFromHandle -Process $process
    }

    $version = Wait-ManagedRuntimeHealthy -Process $process -Identity $identity
    Write-Output "running=true endpoint=$($script:Endpoint) version=$version owner_pid=$($process.Id)"
    if ($ReturnAfterHealthy) { return }

    while ($true) {
        $process.Refresh()
        if ($process.HasExited) {
            Write-Output "running=false owner_pid=$($process.Id) exit_code=$($process.ExitCode)"
            return
        }
        $process.WaitForExit(1000) | Out-Null
    }
}

function Assert-HealthyOwnedRuntime {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$Identity
    )

    $Process.Refresh()
    if ($Process.HasExited) { throw "The held project-owned Ollama process has exited." }
    $currentIdentity = Get-ProcessIdentityFromHandle -Process $Process
    if (-not (Test-OllamaProcessIdentityMatch -Expected $Identity -Actual $currentIdentity)) {
        throw "The held project Process identity no longer matches. No process was changed."
    }
    $version = Get-EndpointVersion
    if (-not $version) { throw "The held project process does not answer at $($script:Endpoint)." }
    return $version
}

function Test-HeldRuntimeProcessOwned {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$Identity
    )

    $Process.Refresh()
    if ($Process.HasExited) { return $true }
    try {
        $currentIdentity = Get-ProcessIdentityFromHandle -Process $Process
        return (Test-OllamaProcessIdentityMatch -Expected $Identity -Actual $currentIdentity)
    }
    catch {
        return $false
    }
}

function Move-CompletedOwnerRecord {
    param([Parameter(Mandatory = $true)]$Identity, [Parameter(Mandatory = $true)][string]$Outcome)

    $state = Read-OwnerState
    if (-not $state) { return }
    if (-not (Test-OllamaProcessIdentityMatch -Expected $Identity -Actual $state)) {
        throw "Owner state no longer identifies this workflow's process; it was preserved."
    }

    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $suffix = [Guid]::NewGuid().ToString("N")
    $directory = Split-Path -Parent $script:StatePath
    $archivePath = Join-Path $directory "server-owner.$Outcome-$stamp-$suffix.json"
    Move-Item -LiteralPath $script:StatePath -Destination $archivePath -ErrorAction Stop
    [Console]::WriteLine("owner_record_archived=$archivePath")
}

function Stop-HeldRuntimeProcess {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$Identity
    )

    $Process.Refresh()
    if (-not $Process.HasExited) {
        $currentIdentity = Get-ProcessIdentityFromHandle -Process $Process
        if (-not (Test-OllamaProcessIdentityMatch -Expected $Identity -Actual $currentIdentity)) {
            throw "The held Process identity does not match; no process was stopped."
        }
        $Process.Kill()
        if (-not $Process.WaitForExit(10000)) {
            throw "The verified workflow-owned process did not stop within 10 seconds."
        }
    }

    if (Test-PortHasListener) {
        throw "A listener remains on port $($script:Port) after stopping this workflow's child. Owner state was preserved."
    }
    Move-CompletedOwnerRecord -Identity $Identity -Outcome "stopped"
    Write-Output "running=false stopped_owner_pid=$($Identity.pid)"
}

function Invoke-Run9BWorkflow {
    $start = { param()
        $resolution = Resolve-OwnerBeforeStart
        Start-ManagedRuntimeProcess -OwnerResolution $resolution
    }
    $getIdentity = { param($Process) Get-ProcessIdentityFromHandle -Process $Process }
    $assertHealthy = { param($Process, $Identity) Wait-ManagedRuntimeHealthy -Process $Process -Identity $Identity }
    $pull = { param($Process, $Identity) Assert-ConfiguredModelInstalled -RuntimeProcess $Process -RuntimeIdentity $Identity }
    $verify = {
        param($Process, $Identity)
        Show-Models -RuntimeProcess $Process -RuntimeIdentity $Identity
        Invoke-SyntheticVerification -RuntimeProcess $Process -RuntimeIdentity $Identity
        Invoke-SyntheticFixtureChat -RuntimeProcess $Process -RuntimeIdentity $Identity
    }
    $testOwned = { param($Process, $Identity) Test-HeldRuntimeProcessOwned -Process $Process -Identity $Identity }
    $stop = { param($Process, $Identity) Stop-HeldRuntimeProcess -Process $Process -Identity $Identity }

    Invoke-OllamaOwnedProcessLifecycle `
        -StartProcess $start `
        -GetIdentity $getIdentity `
        -AssertHealthy $assertHealthy `
        -Pull $pull `
        -Verify $verify `
        -TestOwnedProcess $testOwned `
        -StopProcess $stop
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
        $observation = Get-OwnerProcessObservation -State $state
        if ($observation.status -notin @("absent", "reused")) {
            throw "Could not prove the recorded process is gone. Owner state was preserved."
        }
        $hasListener = Test-PortHasListener
        $archivePath = Move-StaleOllamaOwnerRecordIfSafe `
            -OwnerPath $script:StatePath `
            -ExpectedState $state `
            -ProcessStatus $observation.status `
            -HasListener $hasListener
        Write-Output "running=false stale_owner_record_archived=$archivePath"
        return
    }

    $identity = Get-ProcessIdentityFromHandle -Process $process
    if (-not $identity) { throw "The verified project-owned process exited before stop. Owner state was preserved." }
    $process.Kill()
    if (-not $process.WaitForExit(10000)) {
        throw "The verified project-owned process did not stop within 10 seconds. Owner state was preserved."
    }

    if (Test-PortHasListener) {
        throw "Another listener now occupies port $($script:Port). The owner record was preserved."
    }
    Move-CompletedOwnerRecord -Identity $identity -Outcome "stopped"
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
        $script:LastRuntimeCommandExitCode = $LASTEXITCODE
    }
    finally {
        if ($hadHost) { $env:OLLAMA_HOST = $oldHost } else { Remove-Item Env:OLLAMA_HOST -ErrorAction SilentlyContinue }
        if ($hadModels) { $env:OLLAMA_MODELS = $oldModels } else { Remove-Item Env:OLLAMA_MODELS -ErrorAction SilentlyContinue }
        if ($hadNoCloud) { $env:OLLAMA_NO_CLOUD = $oldNoCloud } else { Remove-Item Env:OLLAMA_NO_CLOUD -ErrorAction SilentlyContinue }
    }
}

function Invoke-ModelPull {
    param($RuntimeProcess, $RuntimeIdentity)

    if ($RuntimeProcess) {
        $null = Assert-HealthyOwnedRuntime -Process $RuntimeProcess -Identity $RuntimeIdentity
    }
    else {
        $null = Get-HealthyManagedRuntime
    }
    Write-Output "pulling model=$($script:ModelName) endpoint=$($script:Endpoint)"
    Invoke-WithRuntimeEnvironment { & $script:Executable pull $script:ModelName }
    $code = $script:LastRuntimeCommandExitCode
    if ($code -ne 0) {
        throw "Ollama model pull failed with exit code $code. The project-owned server remains running."
    }
}

function Show-Models {
    param($RuntimeProcess, $RuntimeIdentity)

    if ($RuntimeProcess) {
        $null = Assert-HealthyOwnedRuntime -Process $RuntimeProcess -Identity $RuntimeIdentity
    }
    else {
        $null = Get-HealthyManagedRuntime
    }
    Invoke-WithRuntimeEnvironment { & $script:Executable list }
    $code = $script:LastRuntimeCommandExitCode
    if ($code -ne 0) {
        throw "Ollama model listing failed with exit code $code."
    }
}

function Assert-ConfiguredModelInstalled {
    param($RuntimeProcess, $RuntimeIdentity)

    if ($RuntimeProcess) {
        $null = Assert-HealthyOwnedRuntime -Process $RuntimeProcess -Identity $RuntimeIdentity
    }
    else {
        $null = Get-HealthyManagedRuntime
    }
    $modelLines = @(Invoke-WithRuntimeEnvironment { & $script:Executable list })
    if ($script:LastRuntimeCommandExitCode -ne 0) {
        throw "Ollama model listing failed with exit code $script:LastRuntimeCommandExitCode."
    }
    $modelPattern = '^\s*' + [regex]::Escape($script:ModelName) + '\s'
    if (-not ($modelLines | Where-Object { $_ -match $modelPattern })) {
        throw "The required local model is not installed. Use the explicit pull action before run-9b."
    }
    Write-Output "model_already_installed=$($script:ModelName)"
}

function Invoke-SyntheticVerification {
    param($RuntimeProcess, $RuntimeIdentity)

    if ($RuntimeProcess) {
        $null = Assert-HealthyOwnedRuntime -Process $RuntimeProcess -Identity $RuntimeIdentity
    }
    else {
        $null = Get-HealthyManagedRuntime
    }
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) { throw "uv was not found for the synthetic Ollama verification." }

    & $uv.Source --cache-dir .uv-cache --offline run python -m ssr_chat verify-ollama `
        --ollama-endpoint $script:Endpoint `
        --ollama-model $script:ModelName
    if ($LASTEXITCODE -ne 0) {
        throw "Synthetic Ollama verification failed with exit code $LASTEXITCODE."
    }
}

function Invoke-SyntheticFixtureChat {
    param(
        [Parameter(Mandatory = $true)]$RuntimeProcess,
        [Parameter(Mandatory = $true)]$RuntimeIdentity
    )

    $null = Assert-HealthyOwnedRuntime -Process $RuntimeProcess -Identity $RuntimeIdentity
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) { throw "uv was not found for the project fixture chat." }

    $runToken = [guid]::NewGuid().ToString("N")
    $messageId = "run-9b-$runToken"
    $stateDirectory = Join-Path $script:RepoRoot (Join-Path ".local-state-ollama" ("run-9b-" + $runToken))
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null

    $fixturePath = Join-Path $script:RepoRoot (Join-Path "fixtures" (Join-Path "events" "general.json"))
    $event = Get-Content -LiteralPath $fixturePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $event.message_id = $messageId
    $event.conversation_id = "run-9b-$runToken"
    $eventJson = $event | ConvertTo-Json -Depth 32 -Compress
    $eventPath = Join-Path $stateDirectory "event.json"
    [System.IO.File]::WriteAllText($eventPath, $eventJson, [System.Text.UTF8Encoding]::new($false))

    $stderrPath = Join-Path $stateDirectory "cli-stderr.log"
    $uvArguments = @(
        "--cache-dir", ".uv-cache",
        "--offline",
        "run", "python", "-m", "ssr_chat", "chat",
        "--input", $eventPath,
        "--state-dir", $stateDirectory,
        "--model-adapter", "ollama",
        "--ollama-endpoint", $script:Endpoint,
        "--ollama-model", $script:ModelName,
        "--ollama-timeout", "300"
    )
    Push-Location $script:RepoRoot
    try {
        $chatOutput = @(& $uv.Source @uvArguments 2> $stderrPath)
        $chatExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    $chatText = $chatOutput -join [Environment]::NewLine
    if ($chatExitCode -ne 0) {
        $stderr = Get-OptionalStderrText -Path $stderrPath
        $failureMessage = "Synthetic fixture chat failed with exit code $chatExitCode."
        $failureMessage += " stdout: $chatText stderr: $stderr state: $stateDirectory"
        throw $failureMessage
    }
    try {
        $result = $chatText | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "Synthetic fixture chat did not return valid JSON. State: $stateDirectory"
    }
    $fixtureGatePassed = $result.status -eq "answered"
    $fixtureGatePassed = $fixtureGatePassed -and ($result.answer -is [string])
    $fixtureGatePassed = $fixtureGatePassed -and -not [string]::IsNullOrWhiteSpace($result.answer)
    $fixtureGatePassed = $fixtureGatePassed -and $result.send_result.status -eq "dry_run_not_sent"
    $fixtureGatePassed = $fixtureGatePassed -and $result.send_result.attempted -eq $false
    $fixtureGatePassed = $fixtureGatePassed -and $result.send_result.public_reply -eq $false
    if (-not $fixtureGatePassed) {
        throw "Synthetic fixture chat failed the answered, nonempty, dry-run, no-public-reply gate. State: $stateDirectory"
    }

    $tracePath = Join-Path $stateDirectory "run_traces.jsonl"
    if (-not (Test-Path -LiteralPath $tracePath -PathType Leaf)) {
        throw "Synthetic fixture chat did not write a run trace. State: $stateDirectory"
    }
    $traces = @(Get-Content -LiteralPath $tracePath -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
    $matchingTraces = @($traces | Where-Object { $_.run_id -eq $result.run_id -and $_.message_id -eq $messageId })
    $traceGatePassed = $matchingTraces.Count -eq 1
    if ($traceGatePassed) {
        $traceGatePassed = $matchingTraces[0].run_status -eq "completed"
        $traceGatePassed = $traceGatePassed -and $matchingTraces[0].send_result.status -eq "dry_run_not_sent"
        $traceGatePassed = $traceGatePassed -and $matchingTraces[0].send_result.attempted -eq $false
        $traceGatePassed = $traceGatePassed -and $matchingTraces[0].send_result.public_reply -eq $false
    }
    if (-not $traceGatePassed) {
        throw "Synthetic fixture chat trace did not confirm one completed dry-run with no public reply. State: $stateDirectory"
    }
    $arrivalPath = Join-Path $stateDirectory "conversation_events.jsonl"
    $arrivals = @(Get-Content -LiteralPath $arrivalPath -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
    $matchingArrivals = @($arrivals | Where-Object { $_.normalized_event.message_id -eq $messageId })
    if ($matchingArrivals.Count -ne 1 -or $matchingArrivals[0].accepted_for_context -ne $true) {
        throw "Synthetic fixture chat did not record one accepted unique event. State: $stateDirectory"
    }
    Write-Output $chatText
    Write-Output "verified_fixture_message_id=$messageId state=$stateDirectory run_id=$($result.run_id)"
}

try {
    switch ($Action) {
        "start" { Start-ManagedRuntime -ReturnAfterHealthy:$ReturnAfterHealthy }
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
        "run-9b" { Invoke-Run9BWorkflow }
    }
}
catch {
    [Console]::Error.WriteLine("ollama-runtime: $($_.Exception.Message)")
    throw
}

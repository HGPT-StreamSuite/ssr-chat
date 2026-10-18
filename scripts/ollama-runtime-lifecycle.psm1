Set-StrictMode -Version Latest

function Test-OllamaProcessIdentityMatch {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Actual
    )

    if ($null -eq $Expected -or $null -eq $Actual) { return $false }
    if ([int]$Expected.pid -ne [int]$Actual.pid) { return $false }

    $expectedPath = [System.IO.Path]::GetFullPath([string]$Expected.executable)
    $actualPath = [System.IO.Path]::GetFullPath([string]$Actual.executable)
    if (-not [string]::Equals($expectedPath, $actualPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }

    try {
        if ($Expected.startedUtc -is [DateTime]) {
            $expectedStart = $Expected.startedUtc.ToUniversalTime().Ticks
        }
        else {
            $expectedStart = [DateTime]::Parse([string]$Expected.startedUtc).ToUniversalTime().Ticks
        }
        if ($Actual.startedUtc -is [DateTime]) {
            $actualStart = $Actual.startedUtc.ToUniversalTime().Ticks
        }
        else {
            $actualStart = [DateTime]::Parse([string]$Actual.startedUtc).ToUniversalTime().Ticks
        }
    }
    catch {
        return $false
    }

    return $expectedStart -eq $actualStart
}

function Move-StaleOllamaOwnerRecordIfSafe {
    param(
        [Parameter(Mandatory = $true)][string]$OwnerPath,
        [Parameter(Mandatory = $true)]$ExpectedState,
        [Parameter(Mandatory = $true)][ValidateSet("absent", "reused")][string]$ProcessStatus,
        [Parameter(Mandatory = $true)][bool]$HasListener
    )

    if ($ProcessStatus -notin @("absent", "reused")) {
        throw "The previous owner process identity is uncertain. Owner state was preserved."
    }
    if ($HasListener) {
        throw "Port 11435 still has a listener. Owner state was preserved; no process was changed."
    }
    if (-not (Test-Path -LiteralPath $OwnerPath -PathType Leaf)) {
        throw "The expected owner record disappeared before it could be quarantined."
    }

    $currentState = Get-Content -LiteralPath $OwnerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not (Test-OllamaProcessIdentityMatch -Expected $ExpectedState -Actual $currentState)) {
        throw "The owner record changed during stale-state inspection. It was preserved."
    }

    $directory = Split-Path -Parent $OwnerPath
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    $suffix = [Guid]::NewGuid().ToString("N")
    $archivePath = Join-Path $directory "server-owner.stale-$stamp-$suffix.json"
    Move-Item -LiteralPath $OwnerPath -Destination $archivePath -ErrorAction Stop
    return $archivePath
}

function Invoke-OllamaOwnedProcessLifecycle {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$StartProcess,
        [Parameter(Mandatory = $true)][scriptblock]$GetIdentity,
        [Parameter(Mandatory = $true)][scriptblock]$AssertHealthy,
        [Parameter(Mandatory = $true)][scriptblock]$Pull,
        [Parameter(Mandatory = $true)][scriptblock]$Verify,
        [Parameter(Mandatory = $true)][scriptblock]$TestOwnedProcess,
        [Parameter(Mandatory = $true)][scriptblock]$StopProcess
    )

    $process = $null
    $identity = $null
    $failure = $null

    try {
        $process = & $StartProcess
        if ($null -eq $process) {
            throw "The runtime start action returned no Process object."
        }

        $identity = & $GetIdentity $process
        if ($null -eq $identity) {
            throw "The launched Process identity could not be captured."
        }

        & $AssertHealthy $process $identity
        & $Pull $process $identity
        & $Verify $process $identity
    }
    catch {
        $failure = $_.Exception
    }
    finally {
        if ($null -ne $process -and $null -ne $identity) {
            try {
                if (-not (& $TestOwnedProcess $process $identity)) {
                    throw "The held Process object no longer verifies as this workflow's child; it was not stopped."
                }
                & $StopProcess $process $identity
            }
            catch {
                if ($null -eq $failure) {
                    $failure = $_.Exception
                }
                else {
                    [Console]::Error.WriteLine("ollama-runtime cleanup: {0}" -f $_.Exception.Message)
                }
            }
        }
    }

    if ($null -ne $failure) { throw $failure }
}

Export-ModuleMember -Function Test-OllamaProcessIdentityMatch, Move-StaleOllamaOwnerRecordIfSafe, Invoke-OllamaOwnedProcessLifecycle

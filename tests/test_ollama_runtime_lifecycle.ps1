$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "..\scripts\ollama-runtime-lifecycle.psm1") -Force

$script:passed = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Sequence {
    param([string[]]$Expected, [string[]]$Actual, [string]$Message)
    if (($Expected -join "|") -ne ($Actual -join "|")) {
        throw "$Message Expected [$($Expected -join ', ')], got [$($Actual -join ', ')]."
    }
}

function Assert-Throws {
    param([scriptblock]$Action, [string]$Message)
    $threw = $false
    try { & $Action } catch { $threw = $true }
    Assert-True $threw $Message
}

function Mark-TestPassed {
    param([string]$Name)
    $script:passed++
    Write-Output "PASS $Name"
}

function Get-LifecycleTestScopeValue { return "script-scope-callback" }

$identity = [ordered]@{
    pid = 41820
    executable = "C:\runtime\ollama.exe"
    startedUtc = "2026-10-18T13:17:04.8989497Z"
}
$sameIdentity = [ordered]@{
    pid = 41820
    executable = "c:\RUNTIME\OLLAMA.exe"
    startedUtc = "2026-10-18T13:17:04.8989497+00:00"
}
$differentIdentity = [ordered]@{
    pid = 41820
    executable = "C:\runtime\ollama.exe"
    startedUtc = "2026-10-18T13:17:05.0000000Z"
}

Assert-True (Test-OllamaProcessIdentityMatch -Expected $identity -Actual $sameIdentity) "Exact process identity should match."
Assert-True (-not (Test-OllamaProcessIdentityMatch -Expected $identity -Actual $differentIdentity)) "A different start time must not match."
Mark-TestPassed "process identity compares path, PID, and exact start time"

$tempBase = [System.IO.Path]::GetFullPath($PSScriptRoot)
$tempRoot = Join-Path $tempBase (".tmp-ollama-runtime-lifecycle-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
try {
    $ownerPath = Join-Path $tempRoot "server-owner.json"
    $identity | ConvertTo-Json | Set-Content -LiteralPath $ownerPath -Encoding UTF8
    $archive = Move-StaleOllamaOwnerRecordIfSafe -OwnerPath $ownerPath -ExpectedState $identity -ProcessStatus absent -HasListener $false
    Assert-True (-not (Test-Path -LiteralPath $ownerPath)) "Stale owner should be moved from the active path."
    Assert-True (Test-Path -LiteralPath $archive) "Stale owner archive should remain recoverable."
    $script:passed++
    Write-Output "PASS absent PID and no listener archive stale owner"

    $ownerPath = Join-Path $tempRoot "server-owner.json"
    $identity | ConvertTo-Json | Set-Content -LiteralPath $ownerPath -Encoding UTF8
    $archive = Move-StaleOllamaOwnerRecordIfSafe -OwnerPath $ownerPath -ExpectedState $identity -ProcessStatus reused -HasListener $false
    Assert-True (Test-Path -LiteralPath $archive) "Reused PID with no listener should archive only the old record."
    $script:passed++
    Write-Output "PASS reused PID and no listener archive stale owner"

    $ownerPath = Join-Path $tempRoot "server-owner.json"
    $identity | ConvertTo-Json | Set-Content -LiteralPath $ownerPath -Encoding UTF8
    Assert-Throws {
        Move-StaleOllamaOwnerRecordIfSafe -OwnerPath $ownerPath -ExpectedState $identity -ProcessStatus absent -HasListener $true
    } "A listener must prevent stale-owner rename."
    Assert-True (Test-Path -LiteralPath $ownerPath) "Owner record must remain when a listener exists."
    $script:passed++
    Write-Output "PASS listener preserves owner record"

    $ownerPath = Join-Path $tempRoot "server-owner.json"
    $identity | ConvertTo-Json | Set-Content -LiteralPath $ownerPath -Encoding UTF8
    $changedState = [ordered]@{
        pid = $identity.pid
        executable = $identity.executable
        startedUtc = "2026-10-18T13:17:05.0000000Z"
    }
    Assert-Throws {
        Move-StaleOllamaOwnerRecordIfSafe -OwnerPath $ownerPath -ExpectedState $changedState -ProcessStatus absent -HasListener $false
    } "An owner record that changed after inspection must not be moved."
    Assert-True (Test-Path -LiteralPath $ownerPath) "Changed owner record must remain."
    $script:passed++
    Write-Output "PASS changed owner record is preserved"

    $events = [System.Collections.Generic.List[string]]::new()
    $fakeProcess = [pscustomobject]@{ marker = "held-object" }
    $start = { $events.Add("start"); return $fakeProcess }.GetNewClosure()
    $getIdentity = { param($Process) if ([object]::ReferenceEquals($Process, $fakeProcess)) { return $identity }; return $null }.GetNewClosure()
    $health = { param($Process, $ProcessIdentity) $events.Add("health") }.GetNewClosure()
    $pull = { param($Process, $ProcessIdentity) $events.Add("pull") }.GetNewClosure()
    $verify = { param($Process, $ProcessIdentity) $events.Add("verify") }.GetNewClosure()
    $isOwned = { param($Process, $ProcessIdentity) $events.Add("ownership"); return [object]::ReferenceEquals($Process, $fakeProcess) }.GetNewClosure()
    $stop = { param($Process, $ProcessIdentity) $events.Add("stop"); if (-not [object]::ReferenceEquals($Process, $fakeProcess)) { throw "Stop did not receive the held process object." } }.GetNewClosure()
    Invoke-OllamaOwnedProcessLifecycle -StartProcess $start -GetIdentity $getIdentity -AssertHealthy $health -Pull $pull -Verify $verify -TestOwnedProcess $isOwned -StopProcess $stop
    Assert-Sequence @("start", "health", "pull", "verify", "ownership", "stop") $events.ToArray() "Lifecycle order is incorrect."
    $script:passed++
    Write-Output "PASS same-session order retains and stops the held Process object"

    $events.Clear()
    $healthFromScriptScope = { param($Process, $ProcessIdentity) $events.Add((Get-LifecycleTestScopeValue)) }
    Invoke-OllamaOwnedProcessLifecycle -StartProcess $start -GetIdentity $getIdentity -AssertHealthy $healthFromScriptScope -Pull $pull -Verify $verify -TestOwnedProcess $isOwned -StopProcess $stop
    Assert-Sequence @("start", "script-scope-callback", "pull", "verify", "ownership", "stop") $events.ToArray() "Runtime callbacks must resolve in the same script session."
    $script:passed++
    Write-Output "PASS same-session callbacks resolve script functions"

    $events.Clear()
    $pullFailure = { param($Process, $ProcessIdentity) $events.Add("pull"); throw "synthetic pull failure" }.GetNewClosure()
    Assert-Throws {
        Invoke-OllamaOwnedProcessLifecycle -StartProcess $start -GetIdentity $getIdentity -AssertHealthy $health -Pull $pullFailure -Verify $verify -TestOwnedProcess $isOwned -StopProcess $stop
    } "A pull failure should remain a workflow failure."
    Assert-Sequence @("start", "health", "pull", "ownership", "stop") $events.ToArray() "Finally cleanup did not run after pull failure."
    $script:passed++
    Write-Output "PASS pull failure still invokes finally cleanup"

    $events.Clear()
    $notOwned = { param($Process, $ProcessIdentity) $events.Add("ownership"); return $false }.GetNewClosure()
    Assert-Throws {
        Invoke-OllamaOwnedProcessLifecycle -StartProcess $start -GetIdentity $getIdentity -AssertHealthy $health -Pull $pull -Verify $verify -TestOwnedProcess $notOwned -StopProcess $stop
    } "An identity mismatch must fail safely."
    Assert-Sequence @("start", "health", "pull", "verify", "ownership") $events.ToArray() "An unverified process must not be stopped."
    $script:passed++
    Write-Output "PASS identity mismatch prevents stop"
}
finally {
    $fullTempRoot = [System.IO.Path]::GetFullPath($tempRoot)
    $fullTempBase = [System.IO.Path]::GetFullPath($tempBase).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullTempRoot.StartsWith($fullTempBase, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a lifecycle test directory outside the temp root."
    }
    if (Test-Path -LiteralPath $fullTempRoot) {
        Remove-Item -LiteralPath $fullTempRoot -Recurse -Force
    }
}

Write-Output "Passed: $script:passed focused lifecycle tests."

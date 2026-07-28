param(
    [Parameter(Mandatory = $true)] [string] $Binary,
    [Parameter(Mandatory = $true)] [string] $TrayBinary,
    [Parameter(Mandatory = $true)] [string] $RouterBinary,
    [Parameter(Mandatory = $true)] [string] $Config,
    [string] $TaskName = "Praxis Body",
    [string] $ServiceName = "PraxisSystemRouter"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)] [string[]] $Arguments
    )
    $output = @(& $FilePath @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        $rendered = $output -join [Environment]::NewLine
        throw "Native command failed ($exitCode): $FilePath $($Arguments -join ' ')`n$rendered"
    }
    return $output
}

function Get-JsonProperty {
    param([object] $Object, [string] $Name)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Set-JsonProperty {
    param([object] $Object, [string] $Name, [object] $Value)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    } else {
        $property.Value = $Value
    }
}

function New-RandomBytes {
    param([int] $Count)
    $bytes = New-Object byte[] $Count
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return $bytes
}

function New-RouterToken {
    return [Convert]::ToBase64String((New-RandomBytes 32)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-RandomHex {
    param([int] $Bytes = 16)
    return -join ((New-RandomBytes $Bytes) | ForEach-Object { $_.ToString('x2') })
}

function Write-JsonAtomic {
    param([string] $Path, [object] $Value, [int] $Depth = 8)
    $temporary = "$Path.tmp-$PID-$(New-RandomHex 4)"
    try {
        [IO.File]::WriteAllText(
            $temporary,
            ($Value | ConvertTo-Json -Depth $Depth),
            (New-Object Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-AllowMask {
    param([System.Security.AccessControl.FileSystemSecurity] $Acl, [string] $Sid)
    [Int64] $mask = 0
    foreach ($rule in $Acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $ruleSid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($ruleSid -eq $Sid) {
            $mask = $mask -bor [Int64] $rule.FileSystemRights
        }
    }
    return $mask
}

function Assert-HardenedAcl {
    param(
        [string] $Path,
        [string] $OwnerSid,
        [System.Security.AccessControl.FileSystemRights] $OwnerRights
    )
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) {
        throw "ACL inheritance is still enabled: $Path"
    }
    $systemSid = 'S-1-5-18'
    $administratorsSid = 'S-1-5-32-544'
    $expected = @($systemSid, $administratorsSid, $OwnerSid) | Select-Object -Unique
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $ruleSid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($expected -notcontains $ruleSid) {
            throw "Unexpected allow ACE for $ruleSid on $Path"
        }
    }
    [Int64] $full = [Security.AccessControl.FileSystemRights]::FullControl
    foreach ($requiredSid in @($systemSid, $administratorsSid)) {
        $mask = Get-AllowMask -Acl $acl -Sid $requiredSid
        if (($mask -band $full) -ne $full) {
            throw "Required FullControl ACE for $requiredSid is missing on $Path"
        }
    }
    [Int64] $ownerRequired = $OwnerRights
    $ownerMask = Get-AllowMask -Acl $acl -Sid $OwnerSid
    if (($ownerMask -band $ownerRequired) -ne $ownerRequired) {
        throw "Owner ACE $OwnerRights is missing on $Path"
    }
    [Int64] $ownerAllowed = $ownerRequired -bor [Int64][Security.AccessControl.FileSystemRights]::Synchronize
    if (($ownerMask -band (-bnot $ownerAllowed)) -ne 0) {
        throw "Owner ACE on $Path grants rights beyond $OwnerRights"
    }
}

function Set-HardenedAcl {
    param(
        [string] $Path,
        [string] $OwnerSid,
        [System.Security.AccessControl.FileSystemRights] $OwnerRights,
        [bool] $OwnerInherits = $false
    )
    $item = Get-Item -LiteralPath $Path
    $system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    $administrators = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $owner = New-Object Security.Principal.SecurityIdentifier($OwnerSid)
    $allow = [Security.AccessControl.AccessControlType]::Allow
    if ($item.PSIsContainer) {
        $security = New-Object Security.AccessControl.DirectorySecurity
        $inherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        $none = [Security.AccessControl.PropagationFlags]::None
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $system, [Security.AccessControl.FileSystemRights]::FullControl, $inherit, $none, $allow
        )))
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $administrators, [Security.AccessControl.FileSystemRights]::FullControl, $inherit, $none, $allow
        )))
        $ownerInheritance = [Security.AccessControl.InheritanceFlags]::None
        if ($OwnerInherits) { $ownerInheritance = $inherit }
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $owner, $OwnerRights, $ownerInheritance, $none, $allow
        )))
    } else {
        $security = New-Object Security.AccessControl.FileSecurity
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $system, [Security.AccessControl.FileSystemRights]::FullControl, $allow
        )))
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $administrators, [Security.AccessControl.FileSystemRights]::FullControl, $allow
        )))
        $security.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $owner, $OwnerRights, $allow
        )))
    }
    $security.SetAccessRuleProtection($true, $false)
    Set-Acl -LiteralPath $Path -AclObject $security
    Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\icacls.exe" -Arguments @($Path, '/verify') | Out-Null
    Assert-HardenedAcl -Path $Path -OwnerSid $OwnerSid -OwnerRights $OwnerRights
}

function Install-VersionedBinary {
    param([string] $Source, [string] $Component, [string] $DestinationRoot)
    $hash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash.ToLowerInvariant()
    $destination = Join-Path $DestinationRoot "$Component-$hash.exe"
    if (Test-Path -LiteralPath $destination) {
        $existingHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($existingHash -ne $hash) {
            throw "Managed binary hash mismatch: $destination"
        }
    } else {
        $temporary = Join-Path $DestinationRoot ".$Component-$hash-$PID.tmp"
        try {
            Copy-Item -LiteralPath $Source -Destination $temporary -Force
            $copiedHash = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($copiedHash -ne $hash) {
                throw "Release binary changed while copying: $Source"
            }
            Move-Item -LiteralPath $temporary -Destination $destination
        } finally {
            if (Test-Path -LiteralPath $temporary) {
                Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
            }
        }
    }
    return $destination
}

if ($ServiceName -notmatch '^[A-Za-z0-9_.-]+$') {
    throw "ServiceName contains unsupported characters: $ServiceName"
}
if ([string]::IsNullOrWhiteSpace($TaskName)) {
    throw "TaskName must not be empty"
}

$principalNow = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principalNow.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Praxis Body installation must run elevated"
}

$sourceBinaryPath = (Resolve-Path -LiteralPath $Binary).Path
$sourceTrayPath = (Resolve-Path -LiteralPath $TrayBinary).Path
$sourceRouterPath = (Resolve-Path -LiteralPath $RouterBinary).Path
$sourceConfigPath = (Resolve-Path -LiteralPath $Config).Path
$configData = Get-Content -LiteralPath $sourceConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$user = $identity.Name
$userSid = $identity.User.Value

$installRoot = Join-Path $env:ProgramData 'Praxis\Body'
$binaryRoot = Join-Path $installRoot 'bin'
$serviceStateDir = Join-Path $installRoot 'state'
$sessionStateDir = Join-Path $env:LOCALAPPDATA 'Praxis\Body'
$configPath = Join-Path $installRoot 'body.json'
$serviceBodyConfigPath = Join-Path $installRoot 'service-body.json'
$routerConfigPath = Join-Path $installRoot 'system-router.json'
$backupRoot = Join-Path $sessionStateDir 'install-backups'

New-Item -ItemType Directory -Force -Path $installRoot, $binaryRoot, $serviceStateDir, $sessionStateDir, $backupRoot | Out-Null
Set-HardenedAcl -Path $installRoot -OwnerSid $userSid -OwnerRights ReadAndExecute
Set-HardenedAcl -Path $binaryRoot -OwnerSid $userSid -OwnerRights ReadAndExecute -OwnerInherits $true
Set-HardenedAcl -Path $serviceStateDir -OwnerSid $userSid -OwnerRights ReadAndExecute -OwnerInherits $true
Set-HardenedAcl -Path $sessionStateDir -OwnerSid $userSid -OwnerRights FullControl -OwnerInherits $true
Set-HardenedAcl -Path $backupRoot -OwnerSid $userSid -OwnerRights FullControl -OwnerInherits $true

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$existingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$installStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if ($existingTask) {
    Export-ScheduledTask -TaskName $TaskName |
        Set-Content -LiteralPath (Join-Path $backupRoot "$installStamp-task.xml") -Encoding Unicode
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}
if ($existingService) {
    Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\sc.exe" -Arguments @('qc', $ServiceName) |
        Set-Content -LiteralPath (Join-Path $backupRoot "$installStamp-service.txt") -Encoding UTF8
    if ($existingService.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
        (Get-Service -Name $ServiceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
}
foreach ($candidate in @($configPath, $serviceBodyConfigPath, $routerConfigPath)) {
    if (Test-Path -LiteralPath $candidate) {
        Copy-Item -LiteralPath $candidate -Destination (Join-Path $backupRoot "$installStamp-$([IO.Path]::GetFileName($candidate))") -Force
    }
}

$managedBodyPath = Install-VersionedBinary -Source $sourceBinaryPath -Component 'praxis-body' -DestinationRoot $binaryRoot
$managedTrayPath = Install-VersionedBinary -Source $sourceTrayPath -Component 'praxis-tray' -DestinationRoot $binaryRoot
$managedRouterPath = Install-VersionedBinary -Source $sourceRouterPath -Component 'praxis-system-router' -DestinationRoot $binaryRoot
foreach ($managedBinary in @($managedBodyPath, $managedTrayPath, $managedRouterPath)) {
    Set-HardenedAcl -Path $managedBinary -OwnerSid $userSid -OwnerRights ReadAndExecute
}

# Pipe addresses and bearer tokens are capabilities too. Rotate all four on every install so a
# stale local process or a copied example config cannot reconnect to the new topology. Never let
# either local router share the remote device credential or the other router's credential.
Set-JsonProperty -Object $configData -Name 'system_router_pipe' -Value ('\\.\pipe\PraxisBodySystem-' + (New-RandomHex 16))
Set-JsonProperty -Object $configData -Name 'interactive_router_pipe' -Value ('\\.\pipe\PraxisBodyInteractive-' + (New-RandomHex 16))
$remoteToken = [string](Get-JsonProperty -Object $configData -Name 'token')
do { $systemRouterToken = New-RouterToken } while ($systemRouterToken -eq $remoteToken)
do { $interactiveRouterToken = New-RouterToken } while (
    $interactiveRouterToken -eq $remoteToken -or $interactiveRouterToken -eq $systemRouterToken
)
Set-JsonProperty -Object $configData -Name 'system_router_token' -Value $systemRouterToken
Set-JsonProperty -Object $configData -Name 'interactive_router_token' -Value $interactiveRouterToken
Set-JsonProperty -Object $configData -Name 'interactive_user_sid' -Value $userSid
Set-JsonProperty -Object $configData -Name 'state_dir' -Value $sessionStateDir
Write-JsonAtomic -Path $configPath -Value $configData

# Only the LocalSystem child owns the remote WSS connection and durable service journal.
$serviceConfigData = ($configData | ConvertTo-Json -Depth 8 | ConvertFrom-Json)
Set-JsonProperty -Object $serviceConfigData -Name 'state_dir' -Value $serviceStateDir
Set-JsonProperty -Object $serviceConfigData -Name 'interactive_user_sid' -Value $userSid
Write-JsonAtomic -Path $serviceBodyConfigPath -Value $serviceConfigData

$routerConfig = [ordered]@{
    body_exe = $managedBodyPath
    body_config = $serviceBodyConfigPath
    pipe = (Get-JsonProperty -Object $configData -Name 'system_router_pipe')
    token = (Get-JsonProperty -Object $configData -Name 'system_router_token')
    allowed_user_sid = $userSid
    log = (Join-Path $serviceStateDir 'service.log')
    session_task = $TaskName
}
Write-JsonAtomic -Path $routerConfigPath -Value $routerConfig -Depth 4
foreach ($installedConfig in @($configPath, $serviceBodyConfigPath, $routerConfigPath)) {
    # Runtime configs include executable paths and bearer material. The interactive owner may
    # read them, but only SYSTEM/an elevated Administrators token may rewrite them.
    Set-HardenedAcl -Path $installedConfig -OwnerSid $userSid -OwnerRights ReadAndExecute
}

# Retire an old tray-owned WSS/session process before registering the new managed paths.
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -like 'praxis-body*.exe' -and
        ($_.CommandLine -like "*$configPath*" -or $_.CommandLine -like "*$sourceConfigPath*") -and
        ($_.CommandLine -match '\bconnect\b' -or $_.CommandLine -match '\bserve-interactive\b')
    } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction Stop | Out-Null }

$serviceCommand = '"' + $managedRouterPath + '" --config "' + $routerConfigPath + '" --service-name "' + $ServiceName + '"'
if ($existingService) {
    Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\sc.exe" -Arguments @(
        'config', $ServiceName, 'binPath=', $serviceCommand, 'start=', 'auto', 'obj=', 'LocalSystem'
    ) | Out-Null
} else {
    Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\sc.exe" -Arguments @(
        'create', $ServiceName, 'binPath=', $serviceCommand, 'start=', 'auto', 'obj=', 'LocalSystem',
        'DisplayName=', 'Praxis Body Service'
    ) | Out-Null
}
Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\sc.exe" -Arguments @(
    'failure', $ServiceName, 'reset=', '86400', 'actions=', 'restart/5000/restart/30000/""/0'
) | Out-Null
Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\sc.exe" -Arguments @('failureflag', $ServiceName, '1') | Out-Null
Invoke-NativeChecked -FilePath "$env:SystemRoot\System32\sc.exe" -Arguments @(
    'description', $ServiceName, 'Elevated transport and execution service for the brainless Praxis Windows body'
) | Out-Null

$trayArgs = "--body `"$managedBodyPath`" --config `"$configPath`" --state-dir `"$sessionStateDir`" --mode session-host"
$action = New-ScheduledTaskAction -Execute $managedTrayPath -Argument $trayArgs
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null

Start-Service -Name $ServiceName -ErrorAction Stop
(Get-Service -Name $ServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$taskDeadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    Start-Sleep -Milliseconds 250
    $installedTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} while ($installedTask.State -ne 'Running' -and [DateTime]::UtcNow -lt $taskDeadline)
if ($installedTask.State -ne 'Running') {
    $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
    throw "Scheduled task '$TaskName' did not stay running; state=$($installedTask.State), lastResult=$($taskInfo.LastTaskResult)"
}

$installedService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction Stop
if ($installedService.State -ne 'Running' -or $installedService.StartMode -ne 'Auto') {
    throw "Service '$ServiceName' failed final state check: state=$($installedService.State), start=$($installedService.StartMode)"
}
if ($installedService.PathName -notlike "*$managedRouterPath*" -or $installedService.PathName -notlike "*--service-name*$ServiceName*") {
    throw "Service '$ServiceName' is not bound to the managed router binary/name: $($installedService.PathName)"
}
$installedAction = @($installedTask.Actions)[0]
if ($installedAction.Execute -ne $managedTrayPath -or $installedAction.Arguments -notlike "*$managedBodyPath*") {
    throw "Scheduled task '$TaskName' is not bound to the managed release binaries"
}
foreach ($managedBinary in @($managedBodyPath, $managedTrayPath, $managedRouterPath)) {
    Set-HardenedAcl -Path $managedBinary -OwnerSid $userSid -OwnerRights ReadAndExecute
}
foreach ($installedConfig in @($configPath, $serviceBodyConfigPath, $routerConfigPath)) {
    Set-HardenedAcl -Path $installedConfig -OwnerSid $userSid -OwnerRights ReadAndExecute
}

Write-Output "Installed hash-versioned Praxis binaries under '$binaryRoot'. '$ServiceName' is the sole elevated WSS owner; '$TaskName' is the headless interactive session host for $user. Backup directory: $backupRoot"

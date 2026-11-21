# G2C set-wait-timeout (Windows port): point the clarify and approval wait
# timeout of the Hermes profiles at one value.
#
# What it manages, per profile:
#   agent.clarify_timeout   the clarify question window (resolver:
#                           tools/clarify_gateway.py resolve_clarify_timeout:
#                           legacy clarify.timeout if set, else
#                           agent.clarify_timeout, else 90; <= 0 unlimited)
#   approvals.timeout       the approval card window (tools/approval_context.py
#                           _get_approval_timeout: default 300, clamped to
#                           agent.deadline.MAX_SAFE_TIMEOUT_S)
#
# Port of runalerts/set-wait-timeout.sh (bash 3.2, macOS/Linux) with the
# same safety posture and exit codes, and ONE documented divergence: a
# negative prior value (e.g. agent.clarify_timeout: -5, the documented
# "<= 0 unlimited" semantic) is displayed, recorded in the state file and
# restored on rollback, while the .sh drops it to null (its printf chokes
# on the leading '-' of the format string, so a .sh rollback would config
# unset the key instead of restoring the negative value; keep the ps1
# behaviour - fixing the .sh is a separate follow-up). Known limits in
# runalerts/README.md "Wait timeout". Windows mapping:
#   Hermes home     HERMES_HOME when set, else %LOCALAPPDATA%\hermes
#   default profile config at <home>\config.yaml; called WITHOUT -p (the
#                   hermes CLI applies flagless calls to the default profile)
#   named profile   config at <home>\profiles\<p>\config.yaml; called with
#                   -p <name>
# The prior values come from the profile's OWN raw config.yaml: the merged
# `hermes config get` can print a seeded default for a key the user layer
# never set, so it cannot be the rollback source of truth. The reader mirrors
# the .sh's line-based block YAML reader (known limits in runalerts/README.md
# "Wait timeout"): integer scalars at the direct-child indent only; flow maps,
# nothing-after-colon, quoted-with-trailing-text, duplicate keys, duplicate
# sections, leaf-shaped lines at a foreign indent and non-numeric scalars all
# refuse fail closed. On Windows the guard refuses a batch when any profile's
# own config.yaml is missing (the .sh guards the profile dir only: with the
# default profile there is no per-profile dir, its home-root config.yaml is
# the unit whose absence a `hermes config set` would paper over).
#
# Writes always go through the hermes CLI (HERMES_BIN when set, else
# hermes.exe resolved with Get-Command -CommandType Application; a bare
# `hermes` can resolve to a non-exe launcher script on Windows, which is why
# the resolution looks for hermes.exe by name). `hermes config set` is always
# followed by a `config get` verification; rollback unsets keys that were
# unset before and sets keys that had a value, tolerated when already unset.
# This script never edits a config file itself and never runs against
# anything but the profiles you name.
#
# Restart: NONE needed. Both values are re-read at every waiter creation
# through the signature-cached config loader (citations in
# runalerts/README.md "Wait timeout"), so the next waiter after
# `hermes config set` uses the new value; waiters created earlier keep the
# window they were born with.
#
# Valid range 60..86400 seconds: below 60 s phone taps cannot land in time,
# above 24 h make a deliberate edit instead of a one-number batch change.
#
# Exit codes: 0 ok, 1 runtime refusal/failure, 2 invalid input (nothing
# touched). PowerShell 5.1 compatible on purpose: users run
# `powershell -ExecutionPolicy Bypass -File <this> ...`, so there is no
# pwsh 7-only syntax here (no ternary, no ??, no &&/||, no -Parallel).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:KeyClarify = 'agent.clarify_timeout'
$script:KeyApprovals = 'approvals.timeout'
$script:KeyLegacy = 'clarify.timeout'
$script:MinWaitTimeout = 60
$script:MaxWaitTimeout = 86400
$script:StateKind = 'g2c-wait-timeout-state'
$script:StateHeaderPattern = '^\{"kind": "g2c-wait-timeout-state", "created_utc": "[^"]{1,40}", "wait_timeout": [0-9]{1,5}\}$'
$script:RecordPattern = '^\{"profile": "([a-z0-9-]{1,64})", "key": "(agent\.clarify_timeout|approvals\.timeout|clarify\.timeout)", "prior": (null|-?[0-9]{1,15})\}$'
$script:Stamp = [DateTime]::Now.ToString('yyyy-MM-dd-HHmmss', [System.Globalization.CultureInfo]::InvariantCulture)
$script:ScriptPath = $PSCommandPath

$script:Apply = $false
$script:AlsoLegacy = $false
$script:WaitTimeout = '600'
$script:ProfilesArg = 'default'
$script:StateFileArg = ''
$script:RollbackPath = ''
$script:HermesHome = ''
$script:HermesBin = ''
$script:ProfileNames = @()
$script:StateDirDefault = ''
$script:StateFileDisplay = ''
$script:LastGetOut = ''

function Fail {
    param([int]$Code, [string]$Message)
    [Console]::Error.WriteLine('FAIL: ' + $Message)
    exit $Code
}

function Trim-SpTab {
    param([string]$Text)
    $t = $Text -replace '^[ \t]+', ''
    $t = $t -replace '[ \t]+$', ''
    return $t
}

function Get-ProfileConfigPath {
    param([string]$ProfileName)
    if ($ProfileName -eq 'default') {
        return (Join-Path $script:HermesHome 'config.yaml')
    }
    $dir = Join-Path $script:HermesHome 'profiles'
    $dir = Join-Path $dir $ProfileName
    return (Join-Path $dir 'config.yaml')
}

function Show-Usage {
    param([switch]$ToErr)
    foreach ($l in $script:UsageLines) {
        if ($ToErr) { [Console]::Error.WriteLine($l) }
        else { [Console]::WriteLine($l) }
    }
}

# --- validation ----------------------------------------------------------------

# Fail-closed value guard, shape-mirrored from the .sh: empty, non-digit,
# zero, leading zero and 16+ digit values die here, before any numeric cast
# can see the string.
function Test-WaitTimeout {
    param([string]$Value)
    if ($Value -cnotmatch '^[1-9][0-9]{0,14}$') {
        Fail 2 ('-WaitTimeout must be an integer in {0}..{1} seconds (got: ''{2}'')' -f $script:MinWaitTimeout, $script:MaxWaitTimeout, $Value)
    }
    if ([long]$Value -lt $script:MinWaitTimeout -or [long]$Value -gt $script:MaxWaitTimeout) {
        Fail 2 ('-WaitTimeout must be in {0}..{1} seconds (got: {2})' -f $script:MinWaitTimeout, $script:MaxWaitTimeout, $Value)
    }
}

# Validates and rewrites -Profiles into a cleaned, case-sensitive list; dies
# on any bad, dash-leading or duplicate name and on any empty item (a
# trailing comma would otherwise be silently dropped). .NET regex character
# classes are code-point ordinal, so the bash locale-collation trap (CI run
# 44902030637) cannot recur.
function Test-Profiles {
    param([string]$Value)
    if ($Value -eq '') {
        Fail 2 '-Profiles needs at least one profile'
    }
    if ($Value.EndsWith(',')) {
        Fail 2 'invalid profile name '''' (empty item after the last comma in -Profiles)'
    }
    $cleaned = @()
    foreach ($name in $Value.Split(',')) {
        if ($name.StartsWith('-')) {
            Fail 2 ('profile name ''{0}'' must not start with ''-'' (it could swallow the next option or reach hermes as a flag)' -f $name)
        }
        if (-not ($name -cmatch '^[a-z0-9-]+$')) {
            Fail 2 ('invalid profile name ''{0}'' (allowed: [a-z0-9-]+)' -f $name)
        }
        if ($cleaned -ccontains $name) {
            Fail 2 ('duplicate profile in -Profiles: {0}' -f $name)
        }
        $cleaned += $name
    }
    $script:ProfileNames = $cleaned
}

# --- raw config.yaml reader (prior values and the legacy guard) ----------------
#
# Mirrors the .sh awk reader token for token (A = absent, V<number> =
# present integer scalar, E = cannot decide, the caller fails closed):
# comments and blank lines carry no mapping content wherever they sit; the
# FIRST content line under a section header fixes the direct-child indent and
# the leaf counts only at exactly that indent; deeper content that never
# names the leaf is skipped; a leaf-shaped line at any other indent refuses.
# Alias/merge keys (<<, *, &) are not consulted: known, inherited blind spot.

function Read-RawToken {
    param([string]$File, [string]$Section, [string]$Leaf)

    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        return @{ Token = 'A'; Value = $null; Reason = '' }
    }

    $leafLine = '^[ \t]+' + $Leaf + '[ \t]*:'
    $leafPrefix = '^[ \t]+' + [regex]::Escape($Leaf) + '[ \t]*:'
    $lines = [System.IO.File]::ReadAllLines($File)
    $ins = $false
    $childIndent = -1
    $nsec = 0
    $done = $false
    $pending = $null

    foreach ($line in $lines) {
        # Document separator resets the tracked section and the child indent.
        if ($line -cmatch '^---( |$)') {
            $ins = $false
            $childIndent = -1
            continue
        }
        # Comments and blank lines can neither fix nor disturb the child indent.
        $probe = Trim-SpTab $line
        if ($probe -eq '' -or $probe.StartsWith('#')) {
            continue
        }

        if ($line -cmatch '^[ \t]') {
            # Indented content line: only meaningful inside a tracked section.
            if ($ins) {
                $ind = ([regex]::Match($line, '^[ \t]+')).Value.Length
                if ($childIndent -eq -1) { $childIndent = $ind }
                if (($ind -ne $childIndent) -and ($line -cmatch $leafLine)) {
                    return @{ Token = 'E'; Value = $null; Reason = ('{0}.{1}: found at indent {2} but the direct children of {3} sit at indent {4} - a nested {1} is not the {0}.{1} value; cannot decide, refusing fail closed' -f $Section, $Leaf, $ind, $Section, $childIndent) }
                }
                if ($line -cmatch $leafLine) {
                    if ($done) {
                        return @{ Token = 'E'; Value = $null; Reason = ('{0}.{1}: duplicate key in {2}' -f $Section, $Leaf, $Section) }
                    }
                    $m = [regex]::Match($line, $leafPrefix)
                    $rest = Trim-SpTab $line.Substring($m.Value.Length)
                    if ($rest -eq '') {
                        return @{ Token = 'E'; Value = $null; Reason = ('{0}.{1}: nothing after the colon (null or nested block)' -f $Section, $Leaf) }
                    }
                    $q = $rest.Substring(0, 1)
                    if (($q -eq '"') -or ($q -eq "'")) {
                        $inner = $rest.Substring(1)
                        $cq = $inner.IndexOf($q)
                        if ($cq -le 0) {
                            return @{ Token = 'E'; Value = $null; Reason = ('{0}.{1}: unsupported quoted scalar' -f $Section, $Leaf) }
                        }
                        $val = $inner.Substring(0, $cq)
                        $tail = $inner.Substring($cq + 1)
                        $ci = $tail.IndexOf('#')
                        if ($ci -ge 0) { $tail = $tail.Substring(0, $ci) }
                        $tail = $tail -replace '[ \t]+$', ''
                        if ($tail -ne '') {
                            return @{ Token = 'E'; Value = $null; Reason = ('{0}.{1}: unsupported trailing text after the quoted value' -f $Section, $Leaf) }
                        }
                    }
                    else {
                        $ci = $rest.IndexOf('#')
                        $val = $rest
                        if ($ci -ge 0) { $val = $rest.Substring(0, $ci) }
                        $val = $val -replace '[ \t]+$', ''
                    }
                    if (-not [regex]::IsMatch($val, '^-?[0-9]+$')) {
                        return @{ Token = 'E'; Value = $null; Reason = ('{0}.{1}: prior value is not an integer scalar (got: {2})' -f $Section, $Leaf, $val) }
                    }
                    $done = $true
                    $pending = $val
                }
            }
            continue
        }

        # Top-level (column 0) content line: section header or stray content.
        $hdr = [regex]::Match($line, '^([A-Za-z0-9_-]+)[ \t]*:').Groups[1].Value
        $ins = $false
        $childIndent = -1
        if ($hdr -eq '') {
            # Dotted key or stray scalar: it ends the section block.
            continue
        }
        if ($hdr -ceq $Section) {
            $nsec = $nsec + 1
            $rem = $line.Substring($line.IndexOf(':') + 1)
            $rem = Trim-SpTab $rem
            if (($rem -eq '') -or ($rem.StartsWith('#'))) {
                $ins = $true
            }
            elseif ($rem.StartsWith('{')) {
                $close = $rem.IndexOf('}')
                if ($close -lt 0) {
                    return @{ Token = 'E'; Value = $null; Reason = ($Section + ': multi-line flow section value is not supported') }
                }
                $flow = $rem.Substring(1, $close - 1)
                if ($flow.Contains($Leaf)) {
                    return @{ Token = 'E'; Value = $null; Reason = ($Section + ': flow section value mentions ' + $Leaf + '; cannot decide the prior value - fix the profile config.yaml by hand') }
                }
                $ins = $false
            }
            else {
                return @{ Token = 'E'; Value = $null; Reason = ($Section + ': unsupported section value (flow/inline/non-map)') }
            }
        }
    }

    if ($nsec -gt 1) {
        return @{ Token = 'E'; Value = $null; Reason = ($Section + ': duplicate section header') }
    }
    if ($done) {
        return @{ Token = 'V'; Value = $pending; Reason = '' }
    }
    return @{ Token = 'A'; Value = $null; Reason = '' }
}

# Read-PriorValue <profile> <label> <key>: prior value (null = unset);
# fails closed (exit 1) when the raw layer cannot be decided.
function Read-PriorValue {
    param([string]$ProfileName, [string]$Label, [string]$Key)
    $dot = $Key.IndexOf('.')
    $sec = $Key.Substring(0, $dot)
    $leaf = $Key.Substring($dot + 1)
    $cfg = Get-ProfileConfigPath $ProfileName
    $tok = Read-RawToken $cfg $sec $leaf
    if ($tok.Token -eq 'E') {
        Fail 1 ('cannot record the prior {0} of profile {1} (raw reader said: E {2}); refusing fail closed - fix the profile config.yaml by hand or run -Apply per profile' -f $Label, $ProfileName, $tok.Reason)
    }
    if ($tok.Token -eq 'V') {
        return $tok.Value
    }
    if ($tok.Token -eq 'A') {
        return $null
    }
    Fail 1 ('cannot record the prior {0} of profile {1} (raw reader returned a broken token: {2}); refusing fail closed' -f $Label, $ProfileName, $tok.Token)
}

# --- hermes call wrappers -------------------------------------------------------

# Each write wrapper echoes the exact command first, so even -Apply keeps a
# transcript a human can replay by hand. Every call goes out through
# $script:HermesBin; the fake-hermes tests pin that no -p is passed for the
# default profile and -p <name> for named profiles.

function Invoke-ConfigSet {
    param([string]$ProfileName, [string]$Key, [string]$Value)
    if ($ProfileName -eq 'default') {
        [Console]::WriteLine('  ' + $script:HermesBin + ' config set ' + $Key + ' ' + $Value)
        & $script:HermesBin config set $Key $Value
    }
    else {
        [Console]::WriteLine('  ' + $script:HermesBin + ' -p ' + $ProfileName + ' config set ' + $Key + ' ' + $Value)
        & $script:HermesBin -p $ProfileName config set $Key $Value
    }
}

function Invoke-ConfigUnset {
    param([string]$ProfileName, [string]$Key)
    if ($ProfileName -eq 'default') {
        [Console]::WriteLine('  ' + $script:HermesBin + ' config unset ' + $Key)
        & $script:HermesBin config unset $Key
    }
    else {
        [Console]::WriteLine('  ' + $script:HermesBin + ' -p ' + $ProfileName + ' config unset ' + $Key)
        & $script:HermesBin -p $ProfileName config unset $Key
    }
}

function Invoke-ConfigGet {
    # Captures stdout, drops stderr (the real CLI prints "Config key not set"
    # there when the key is unset); the result lands in $script:LastGetOut.
    # 2>$null turns stderr lines into error records on PS 5.1, so relax
    # $ErrorActionPreference around it.
    param([string]$ProfileName, [string]$Key)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($ProfileName -eq 'default') {
            $out = @(& $script:HermesBin config get $Key 2>$null)
        }
        else {
            $out = @(& $script:HermesBin -p $ProfileName config get $Key 2>$null)
        }
    }
    finally {
        $ErrorActionPreference = $prev
    }
    $script:LastGetOut = ($out -join "`n")
}

function Test-ConfigVerify {
    # Test-ConfigVerify <profile> <key> <expected>
    param([string]$ProfileName, [string]$Key, [string]$Expected)
    Invoke-ConfigGet $ProfileName $Key
    if ($script:LastGetOut -ne $Expected) {
        Fail 1 ('verify failed for profile {0} {1}: config get printed ''{2}'', expected ''{3}'' - the state file has the prior values, roll back or fix by hand' -f $ProfileName, $Key, $script:LastGetOut, $Expected)
    }
}

# --- state file -----------------------------------------------------------------

function Test-StateHeaderLine {
    # True only when <line> IS the state file header record, anchored at both
    # ends (the exact shape -Apply writes). A line that merely CONTAINS the
    # kind substring falls through to the record parser and is refused there:
    # an incomplete rollback is worse than none.
    param([string]$Line)
    return [bool]($Line -cmatch $script:StateHeaderPattern)
}

function ConvertFrom-StateRecordLine {
    # Returns a hashtable {Profile, Key, Prior} for a restore record, or
    # $null for any other shape (header lines and blank lines are skipped by
    # the caller). Case-sensitive on purpose: prior must be lowercase null.
    param([string]$Line)
    $m = [regex]::Match($Line, $script:RecordPattern)
    if (-not $m.Success) {
        return $null
    }
    return @{ Profile = $m.Groups[1].Value; Key = $m.Groups[2].Value; Prior = $m.Groups[3].Value }
}

function Protect-StateFile {
    # Make the state file private to the current user: owner-only ACL on
    # Windows, 0600 elsewhere (pwsh on POSIX runs the suite too).
    # PS 5.1 (.NET Framework) exposes [System.IO.File]::SetAccessControl;
    # pwsh 7 on Windows has to go through System.IO.FileSystemAclExtensions
    # instead (the static does not exist there, and -Apply must work under
    # both interpreters).
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($env:OS -eq 'Windows_NT') {
        try {
            $sec = New-Object System.Security.AccessControl.FileSecurity
            $sec.SetAccessRuleProtection($true, $false)
            $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
            $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
            $sec.AddAccessRule($rule)
            if ($PSVersionTable.PSVersion.Major -ge 6) {
                $info = New-Object System.IO.FileInfo($full)
                [System.IO.FileSystemAclExtensions]::SetAccessControl($info, $sec)
            }
            else {
                [System.IO.File]::SetAccessControl($full, $sec)
            }
        }
        catch {
            Fail 1 ('cannot restrict the state file permissions: {0} ({1})' -f $Path, $_.Exception.Message)
        }
    }
    else {
        & chmod 600 $full
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            Fail 1 ('cannot restrict the state file permissions: {0} (chmod exit {1})' -f $Path, $rc)
        }
    }
}

# --- apply ----------------------------------------------------------------------

function Invoke-Apply {
    # L2 guard: refuse the whole batch up front, before anything is recorded
    # or set, when a named profile's config.yaml is missing (a typo would
    # make hermes config set create a junk profile) or the default profile's
    # home-root config.yaml is missing.
    $missing = ''
    foreach ($p in $script:ProfileNames) {
        $cfg = Get-ProfileConfigPath $p
        if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) {
            $missing = $missing + ' ' + $p
        }
    }
    if ($missing -ne '') {
        Fail 1 ('profile config file(s) missing under the Hermes home:{0} (hermes config set would create a junk profile for a typo) - create the profile config(s) first or fix -Profiles' -f $missing)
    }

    # Pass 1: read every prior BEFORE touching anything; build the state file
    # content in memory; refuse the whole batch on any legacy key (unless
    # -AlsoLegacy) or on any undecidable raw value.
    $records = New-Object System.Collections.Generic.List[string]
    $legacyOffenders = 0
    $legacyRecorded = @{}
    foreach ($p in $script:ProfileNames) {
        $c = Read-PriorValue $p $script:KeyClarify $script:KeyClarify
        $a = Read-PriorValue $p $script:KeyApprovals $script:KeyApprovals
        $l = Read-PriorValue $p ('legacy ' + $script:KeyLegacy) $script:KeyLegacy
        $priorC = 'null'
        $priorA = 'null'
        $priorL = 'null'
        if ($null -ne $c) { $priorC = $c }
        if ($null -ne $a) { $priorA = $a }
        if ($null -ne $l) { $priorL = $l }
        $records.Add('{"profile": "' + $p + '", "key": "' + $script:KeyClarify + '", "prior": ' + $priorC + '}')
        $records.Add('{"profile": "' + $p + '", "key": "' + $script:KeyApprovals + '", "prior": ' + $priorA + '}')
        $dispC = '(unset)'
        $dispA = '(unset)'
        $dispL = '(unset)'
        if ($null -ne $c) { $dispC = $c }
        if ($null -ne $a) { $dispA = $a }
        if ($null -ne $l) { $dispL = $l }
        if ($null -ne $l) {
            if (-not $script:AlsoLegacy) {
                [Console]::Error.WriteLine(('FAIL: refusing to apply: profile {0} still sets the legacy key {1}={2} (it overrides {3}, so the new value would be silently ignored there)' -f $p, $script:KeyLegacy, $dispL, $script:KeyClarify))
                $legacyOffenders = $legacyOffenders + 1
            }
            else {
                $records.Add('{"profile": "' + $p + '", "key": "' + $script:KeyLegacy + '", "prior": ' + $priorL + '}')
                $legacyRecorded[$p] = $true
            }
        }
        [Console]::WriteLine(('    {0,-14} {1}={2}  {3}={4}  legacy {5}={6}' -f $p, $script:KeyClarify, $dispC, $script:KeyApprovals, $dispA, $script:KeyLegacy, $dispL))
    }
    if ($legacyOffenders -gt 0) {
        [Console]::Error.WriteLine(('FAIL: {0} profile(s) with a legacy {1} key; nothing was changed. Either remove the legacy key from those profiles by hand, or rerun with -AlsoLegacy to point the legacy key at the same value (its prior value gets recorded and rolled back).' -f $legacyOffenders, $script:KeyLegacy))
        exit 1
    }
    $header = '{"kind": "' + $script:StateKind + '", "created_utc": "' + ([DateTime]::Now.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [System.Globalization.CultureInfo]::InvariantCulture)) + '", "wait_timeout": ' + $script:WaitTimeout + '}'

    # State file: complete before any mutation; never overwrite a record.
    $stateFile = $script:StateFileArg
    if (-not $stateFile) {
        if (-not $script:StateDirDefault) {
            Fail 1 'cannot resolve the default state dir: pass -State, set G2C_STATE_DIR, or run on Windows where LOCALAPPDATA exists'
        }
        $stateFile = Join-Path $script:StateDirDefault ('wait-timeout-state-' + $script:Stamp + '.jsonl')
    }
    if (Test-Path -LiteralPath $stateFile) {
        Fail 1 ('-State file already exists: {0} (refusing to overwrite a rollback record)' -f $stateFile)
    }
    $stateDir = Split-Path -Parent $stateFile
    if ($stateDir -and -not (Test-Path -LiteralPath $stateDir -PathType Container)) {
        try {
            New-Item -ItemType Directory -Path $stateDir -Force -ErrorAction Stop | Out-Null
        }
        catch {
            Fail 1 ('cannot create the state dir {0} ({1})' -f $stateDir, $_.Exception.Message)
        }
    }
    $records.Insert(0, $header)
    $content = ($records -join "`n") + "`n"
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($stateFile), $content, $utf8NoBom)
    Protect-StateFile $stateFile

    [Console]::WriteLine(('applying wait timeout {0}s to profiles: {1}' -f $script:WaitTimeout, ($script:ProfileNames -join ',')))
    [Console]::WriteLine(('state file: {0}' -f $stateFile))
    [Console]::WriteLine(('rollback later: powershell -ExecutionPolicy Bypass -File "{0}" -Rollback "{1}"' -f $script:ScriptPath, $stateFile))

    # Pass 2: apply + verify per profile; every set command is echoed first.
    foreach ($p in $script:ProfileNames) {
        [Console]::WriteLine(('profile: {0}' -f $p))
        Invoke-ConfigSet $p $script:KeyClarify $script:WaitTimeout
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            Fail 1 ('config set {0} failed for profile {1} (exit {2})' -f $script:KeyClarify, $p, $rc)
        }
        Invoke-ConfigSet $p $script:KeyApprovals $script:WaitTimeout
        $rc = $LASTEXITCODE
        if ($rc -ne 0) {
            Fail 1 ('config set {0} failed for profile {1} (exit {2})' -f $script:KeyApprovals, $p, $rc)
        }
        Test-ConfigVerify $p $script:KeyClarify $script:WaitTimeout
        Test-ConfigVerify $p $script:KeyApprovals $script:WaitTimeout
        [Console]::WriteLine(('  verify ok: {0}={1}  {2}={3}' -f $script:KeyClarify, $script:WaitTimeout, $script:KeyApprovals, $script:WaitTimeout))
        if ($script:AlsoLegacy -and $legacyRecorded.ContainsKey($p)) {
            Invoke-ConfigSet $p $script:KeyLegacy $script:WaitTimeout
            $rc = $LASTEXITCODE
            if ($rc -ne 0) {
                Fail 1 ('config set {0} failed for profile {1} (exit {2})' -f $script:KeyLegacy, $p, $rc)
            }
            Test-ConfigVerify $p $script:KeyLegacy $script:WaitTimeout
            [Console]::WriteLine(('  verify ok: {0}={1} (legacy)' -f $script:KeyLegacy, $script:WaitTimeout))
        }
    }
    [Console]::WriteLine(('done: wait timeout {0}s set and verified on: {1}' -f $script:WaitTimeout, ($script:ProfileNames -join ',')))
    [Console]::WriteLine('restart: none needed - the next waiter re-reads the profile config (signature-cached); waiters created before now keep their window. Citations in runalerts/README.md "Wait timeout".')
}

# --- rollback -------------------------------------------------------------------

function Invoke-Rollback {
    param([string]$RollbackPath)
    # N2: a state file whose last line has no newline was truncated mid-write;
    # the record loop would silently restore one record less than was
    # recorded. Refuse before touching anything.
    $full = [System.IO.Path]::GetFullPath($RollbackPath)
    $bytes = [System.IO.File]::ReadAllBytes($full)
    if (($bytes.Length -gt 0) -and ($bytes[$bytes.Length - 1] -ne 10)) {
        Fail 1 ('state file is incomplete: the last line does not end with a newline (truncated?): {0} - refusing: an incomplete rollback is worse than none' -f $RollbackPath)
    }
    $lines = [System.IO.File]::ReadAllLines($full)
    $ok = 0
    $lineno = 0
    foreach ($line in $lines) {
        $lineno = $lineno + 1
        $s = [string]$line
        if ($s -eq '') { continue }
        # N3: the header is skipped on the EXACT header shape only; a line
        # that merely contains the kind text falls through to the record
        # parser and is refused instead of being silently skipped.
        if (Test-StateHeaderLine $s) { continue }
        $rec = ConvertFrom-StateRecordLine $s
        if ($null -eq $rec) {
            Fail 1 ('state file line {0} is not understood: {1} (refusing: an incomplete rollback is worse than none)' -f $lineno, $s)
        }
        $p = $rec.Profile
        $k = $rec.Key
        $v = $rec.Prior
        if ($v -eq 'null') {
            [Console]::WriteLine(('profile: {0}  {1} -> unset' -f $p, $k))
            Invoke-ConfigUnset $p $k
            $rc = $LASTEXITCODE
            if ($rc -eq 0) {
                [Console]::WriteLine('  unset ok')
            }
            elseif ($rc -eq 1) {
                [Console]::WriteLine('  already unset (ok)')
            }
            else {
                Fail 1 ('config unset failed for profile {0} key {1} (exit {2}) - inspect that profile by hand' -f $p, $k, $rc)
            }
        }
        else {
            [Console]::WriteLine(('profile: {0}  {1} -> {2}' -f $p, $k, $v))
            Invoke-ConfigSet $p $k $v
            $rc = $LASTEXITCODE
            if ($rc -ne 0) {
                Fail 1 ('config set failed for profile {0} key {1} (exit {2})' -f $p, $k, $rc)
            }
            Test-ConfigVerify $p $k $v
            [Console]::WriteLine(('  restore ok: {0}={1}' -f $k, $v))
        }
        $ok = $ok + 1
    }
    if ($ok -eq 0) {
        Fail 1 ('state file has no restore line(s): {0}' -f $RollbackPath)
    }
    [Console]::WriteLine(('rollback done: {0} key(s) restored; waiters created while the temporary value was live keep that window.' -f $ok))
}

# --- dry run --------------------------------------------------------------------

function Invoke-DryRun {
    [Console]::WriteLine('DRY RUN: nothing written, nothing run (use -Apply to execute)')
    [Console]::WriteLine(('wait timeout: {0}s' -f $script:WaitTimeout))
    [Console]::WriteLine(('profiles: {0}' -f ($script:ProfileNames -join ',')))
    [Console]::WriteLine(('state file (on -Apply): {0}' -f $script:StateFileDisplay))
    [Console]::WriteLine('')
    [Console]::WriteLine('prior values (read from the raw profile config.yaml, not probed from hermes):')
    foreach ($p in $script:ProfileNames) {
        $cfg = Get-ProfileConfigPath $p
        # Transcript parity with -Apply: the batch refuses when a named
        # profile config is missing, so the dry run prints the same note here
        # instead of commands that nothing would run.
        if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) {
            [Console]::WriteLine(('    {0,-14} profile config missing: {1}' -f $p, $cfg))
            continue
        }
        $c = Read-PriorValue $p $script:KeyClarify $script:KeyClarify
        $a = Read-PriorValue $p $script:KeyApprovals $script:KeyApprovals
        $l = Read-PriorValue $p ('legacy ' + $script:KeyLegacy) $script:KeyLegacy
        $dispC = '(unset)'
        $dispA = '(unset)'
        $dispL = '(unset)'
        if ($null -ne $c) { $dispC = $c }
        if ($null -ne $a) { $dispA = $a }
        if ($null -ne $l) { $dispL = $l }
        [Console]::WriteLine(('    {0,-14} {1}={2}  {3}={4}  legacy {5}={6}' -f $p, $script:KeyClarify, $dispC, $script:KeyApprovals, $dispA, $script:KeyLegacy, $dispL))
    }
    [Console]::WriteLine('')
    [Console]::WriteLine('commands per profile:')
    foreach ($p in $script:ProfileNames) {
        $cfg = Get-ProfileConfigPath $p
        if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) {
            [Console]::WriteLine(('    {0,-14} profile config missing: {1}' -f $p, $cfg))
            continue
        }
        if ($p -eq 'default') {
            [Console]::WriteLine('  ' + $script:HermesBin + ' config set ' + $script:KeyClarify + ' ' + $script:WaitTimeout)
            [Console]::WriteLine('  ' + $script:HermesBin + ' config set ' + $script:KeyApprovals + ' ' + $script:WaitTimeout)
            [Console]::WriteLine('  ' + $script:HermesBin + ' config get ' + $script:KeyClarify + '   # verify: prints ' + $script:WaitTimeout)
            [Console]::WriteLine('  ' + $script:HermesBin + ' config get ' + $script:KeyApprovals + '    # verify: prints ' + $script:WaitTimeout)
        }
        else {
            [Console]::WriteLine('  ' + $script:HermesBin + ' -p ' + $p + ' config set ' + $script:KeyClarify + ' ' + $script:WaitTimeout)
            [Console]::WriteLine('  ' + $script:HermesBin + ' -p ' + $p + ' config set ' + $script:KeyApprovals + ' ' + $script:WaitTimeout)
            [Console]::WriteLine('  ' + $script:HermesBin + ' -p ' + $p + ' config get ' + $script:KeyClarify + '   # verify: prints ' + $script:WaitTimeout)
            [Console]::WriteLine('  ' + $script:HermesBin + ' -p ' + $p + ' config get ' + $script:KeyApprovals + '    # verify: prints ' + $script:WaitTimeout)
        }
    }
    [Console]::WriteLine('')
    [Console]::WriteLine('restart: none needed - the gateway re-reads these values at every waiter creation (signature-cached config load, no process-level cache of the value); a waiter created before the change keeps its old window. Citations in runalerts/README.md "Wait timeout".')
}

# --- usage ----------------------------------------------------------------------

$usage = @'
set-wait-timeout.ps1: set the clarify/approval wait timeout of the Hermes
profiles on Windows.

Usage:
  powershell -ExecutionPolicy Bypass -File set-wait-timeout.ps1 [-WaitTimeout SECONDS] [-Profiles a,b,c]
                            [-Apply] [-State FILE] [-AlsoLegacy]
  powershell -ExecutionPolicy Bypass -File set-wait-timeout.ps1 -Rollback FILE

Options:
  -WaitTimeout SECONDS   the new window for BOTH keys (default 600).
                         Valid: 60..86400 (rejects empty, non-digits, 0,
                         leading zeros, 16+ digits; exit 2, nothing touched).
                         Below 60 s phone taps cannot land in time; above
                         24 h make a deliberate edit instead.
  -Profiles a,b,c        bare profile ids, [a-z0-9-]+, case-sensitive, no
                         leading dash (default: default - Windows Hermes has
                         only the default profile today)
  -Apply                 run the config sets (default: DRY RUN, prints the
                         exact commands and touches nothing)
  -State FILE            JSONL state file for -Apply (default:
                         %LOCALAPPDATA%\G2C\wait-timeout-state-<stamp>.jsonl,
                         private to the user; records the prior raw-layer
                         value of every key per profile, null = it was not
                         set; G2C_STATE_DIR overrides the default dir, for
                         test sandboxes)
  -AlsoLegacy            a profile that still sets the legacy
                         'clarify.timeout' makes the script refuse (that key
                         overrides agent.clarify_timeout and would silently
                         win). With -AlsoLegacy the script proceeds AND
                         points that legacy key at the same value for
                         profiles where it already exists (its prior value
                         is recorded too); unset legacy keys are left alone
                         (agent.clarify_timeout governs them).
  -Rollback FILE         restore the prior values recorded in FILE (config
                         set for a recorded value, config unset for null; an
                         "already unset" is tolerated and reported as ok)
  HERMES_BIN             hermes binary to call; when unset, hermes.exe is
                         resolved with Get-Command -CommandType Application
                         (a bare 'hermes' can resolve to a non-exe launcher
                         script on Windows, so the resolution looks for
                         hermes.exe by name)
  HERMES_HOME            Hermes home dir (default: %LOCALAPPDATA%\hermes);
                         the default profile's config is <home>\config.yaml,
                         a named profile's is <home>\profiles\<p>\config.yaml
  G2C_STATE_DIR          overrides the default state file dir

Exit codes: 0 ok, 1 refusal/failure, 2 invalid input.
No gateway restart is needed (see runalerts/README.md "Wait timeout").
'@
$script:UsageLines = $usage -split "\r?\n"

# --- arg parsing ----------------------------------------------------------------

# Manual parsing keeps the exit-code contract in the script's hands: bad
# values, missing values and unknown arguments must die with exit 2 before
# anything happens, exactly like the .sh.
$i = 0
while ($i -lt $args.Count) {
    $a = [string]$args[$i]
    $i = $i + 1
    if (($a -eq '-Help') -or ($a -eq '-h') -or ($a -eq '--help')) {
        Show-Usage
        exit 0
    }
    elseif ($a -eq '-Apply') {
        $script:Apply = $true
    }
    elseif ($a -eq '-AlsoLegacy') {
        $script:AlsoLegacy = $true
    }
    elseif ($a -eq '-WaitTimeout') {
        if ($i -ge $args.Count) { Fail 2 '-WaitTimeout needs a value' }
        $script:WaitTimeout = [string]$args[$i]
        $i = $i + 1
    }
    elseif ($a -eq '-Profiles') {
        if ($i -ge $args.Count) { Fail 2 '-Profiles needs a value' }
        $script:ProfilesArg = [string]$args[$i]
        $i = $i + 1
    }
    elseif ($a -eq '-State') {
        if ($i -ge $args.Count) { Fail 2 '-State needs a value' }
        $script:StateFileArg = [string]$args[$i]
        $i = $i + 1
    }
    elseif ($a -eq '-Rollback') {
        if ($i -ge $args.Count) { Fail 2 '-Rollback needs a value' }
        $script:RollbackPath = [string]$args[$i]
        $i = $i + 1
    }
    else {
        [Console]::Error.WriteLine('FAIL: unknown argument: ' + $a)
        Show-Usage -ToErr
        exit 2
    }
}

Test-WaitTimeout $script:WaitTimeout
Test-Profiles $script:ProfilesArg

# Hermes binary: HERMES_BIN wins; else resolve hermes.exe (Application type)
# on PATH. The resolution stays silent and runs BEFORE the rollback dispatch:
# the .sh sets HERMES_BIN=hermes at the top, so a rollback never sees an empty
# command; with nothing resolved the literal name is a placeholder that fails
# per-call, exactly like the .sh's command-not-found. The dry-run note and the
# -Apply refusals run later, after the rollback branch (same position as the
# .sh's note block).
$script:HermesBin = 'hermes.exe'
$script:HermesBinResolved = $false
if ($env:HERMES_BIN) {
    $script:HermesBin = $env:HERMES_BIN
    $script:HermesBinResolved = $true
}
else {
    try {
        $app = Get-Command -Name 'hermes.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    catch {
        $app = $null
    }
    if ($app) {
        $binPath = [string]$app.Source
        if (-not $binPath) { $binPath = [string]$app.Path }
        if ($binPath) {
            $script:HermesBin = $binPath
            $script:HermesBinResolved = $true
        }
    }
}

if ($script:RollbackPath) {
    if ($script:Apply) {
        Fail 2 '-Apply and -Rollback are mutually exclusive'
    }
    if ($script:StateFileArg) {
        Fail 2 '-State and -Rollback are mutually exclusive'
    }
    if ($script:AlsoLegacy -or ($script:WaitTimeout -ne '600')) {
        Fail 2 '-AlsoLegacy and -WaitTimeout do not apply to -Rollback'
    }
    if (-not (Test-Path -LiteralPath $script:RollbackPath -PathType Leaf)) {
        Fail 1 ('state file not found: {0}' -f $script:RollbackPath)
    }
    $rlines = [System.IO.File]::ReadAllLines([System.IO.Path]::GetFullPath($script:RollbackPath))
    $first = ''
    if ($rlines.Count -gt 0) { $first = [string]$rlines[0] }
    if (-not (Test-StateHeaderLine $first)) {
        Fail 1 ('state file does not look like a {0} record: {1}' -f $script:StateKind, $script:RollbackPath)
    }
    Invoke-Rollback $script:RollbackPath
    exit 0
}

if ($script:StateFileArg -and -not $script:Apply) {
    Fail 2 '-State only makes sense with -Apply'
}

if (-not $env:HERMES_HOME) {
    if (-not $env:LOCALAPPDATA) {
        Fail 2 'cannot resolve the Hermes home: set HERMES_HOME (on Windows LOCALAPPDATA provides the default %LOCALAPPDATA%\hermes)'
    }
    $script:HermesHome = Join-Path $env:LOCALAPPDATA 'hermes'
}
else {
    $script:HermesHome = $env:HERMES_HOME
}

$script:StateDirDefault = $env:G2C_STATE_DIR
if (-not $script:StateDirDefault) {
    if ($env:LOCALAPPDATA) {
        $script:StateDirDefault = Join-Path $env:LOCALAPPDATA 'G2C'
    }
}
$script:StateFileDisplay = ''
if ($script:StateFileArg) {
    $script:StateFileDisplay = $script:StateFileArg
}
elseif ($script:StateDirDefault) {
    $script:StateFileDisplay = Join-Path $script:StateDirDefault ('wait-timeout-state-' + $script:Stamp + '.jsonl')
}

# Hermes binary note / refusals (after the rollback branch, like the .sh):
# a dry run without a real binary still prints its commands; -Apply refuses.
if (-not $script:HermesBinResolved) {
    if ($script:Apply) {
        Fail 2 'no hermes.exe found on PATH and HERMES_BIN is not set; set HERMES_BIN to the hermes launcher you want'
    }
    [Console]::WriteLine('note: no hermes.exe found on PATH and HERMES_BIN is not set (irrelevant for a dry run: the commands are printed, not run)')
}
if ($script:Apply) {
    if (-not (Test-Path -LiteralPath $script:HermesBin -PathType Leaf)) {
        Fail 2 ('HERMES_BIN={0} is not an executable file' -f $script:HermesBin)
    }
}

if ($script:Apply) {
    Invoke-Apply
    exit 0
}

Invoke-DryRun
exit 0

# Sets the deployment's private proper nouns and the archive origin.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set-private-config.ps1
#
# Every prompt is hidden and optional: press Enter to keep whatever is already
# configured. Nothing is echoed or written to disk; only NAMES are printed.
# Safe to re-run.
#
# Where each value lands, and why:
#   CF_ARCHIVE_ORIGIN        repository VARIABLE (read by scripts/deploy-worker.ps1)
#   DIGEST_ARCHIVE_ORIGIN    Worker secret -- the value the Worker validates
#                            ingest against. Workers cannot read repo variables,
#                            which is why this one exists twice.
#   BANK_NAME / BANK_URL / COLLEGE_NAME / COLLEGE_URL
#                            repository secrets, read as env by
#                            scripts/private_values.py in daily-news.yml and
#                            breaking-monitor.yml.

$ErrorActionPreference = 'Stop'
$WorkerConfig = if (Test-Path 'deploy/wrangler.local.toml') { 'deploy/wrangler.local.toml' } else { 'deploy/wrangler.toml' }

function Read-Hidden([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Test-AbsoluteUrl([string]$Value) {
    # The Worker refuses an origin without a dotted host, so refuse it here too
    # rather than storing a value that will be logged as malformed at ingest time.
    $uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri)) { return $false }
    if ($uri.Scheme -ne 'https' -and $uri.Scheme -ne 'http') { return $false }
    return ($uri.Host.Contains('.') -and -not $uri.Host.EndsWith('.'))
}

function Set-RepoSecret([string]$Name, [string]$Value) {
    $Value | & gh secret set $Name
    if ($LASTEXITCODE -ne 0) { throw "gh secret set $Name failed" }
    Write-Host "  set repo secret: $Name"
}

# --- the archive origin -----------------------------------------------------
$origin = Read-Hidden 'Archive origin, i.e. the https://<project>.pages.dev host (hidden, Enter to keep)'
if (-not [string]::IsNullOrWhiteSpace($origin)) {
    $origin = $origin.Trim().TrimEnd('/')
    if (-not (Test-AbsoluteUrl $origin)) {
        throw 'that is not an absolute http(s) URL with a dotted hostname; nothing was changed'
    }
    try {
        & gh variable set CF_ARCHIVE_ORIGIN --body $origin
        if ($LASTEXITCODE -ne 0) { throw 'gh variable set CF_ARCHIVE_ORIGIN failed' }
        Write-Host '  set repo variable: CF_ARCHIVE_ORIGIN'

        $origin | & wrangler.cmd secret put DIGEST_ARCHIVE_ORIGIN -c $WorkerConfig
        if ($LASTEXITCODE -ne 0) { throw 'wrangler secret put DIGEST_ARCHIVE_ORIGIN failed' }
        Write-Host '  set Worker secret: DIGEST_ARCHIVE_ORIGIN'
    } finally {
        $origin = $null
    }
}

# --- the institution values -------------------------------------------------
foreach ($spec in @(
    @{ Name = 'BANK_NAME';    Prompt = "Bank display name, e.g. 'My Bank (Branch) (hidden, Enter to keep)'" },
    @{ Name = 'BANK_URL';     Prompt = 'Bank notices/homepage URL (hidden, Enter to keep)' },
    @{ Name = 'COLLEGE_NAME'; Prompt = "College display name, e.g. 'My Campus / CAMPUS (hidden, Enter to keep)'" },
    @{ Name = 'COLLEGE_URL';  Prompt = 'College notices/homepage URL (hidden, Enter to keep)' }
)) {
    $value = Read-Hidden $spec.Prompt
    if ([string]::IsNullOrWhiteSpace($value)) { continue }
    $value = $value.Trim()
    if ($spec.Name -like '*_URL' -and -not (Test-AbsoluteUrl $value)) {
        Write-Warning ("{0}: not an absolute http(s) URL with a dotted hostname -- SKIPPED, unchanged" -f $spec.Name)
        continue
    }
    try {
        Set-RepoSecret $spec.Name $value
    } finally {
        $value = $null
    }
}

Write-Host ''
Write-Host 'Done. Configured names only (values were never printed):'
& gh variable list | Select-String -Pattern 'CF_ARCHIVE_ORIGIN'
& gh secret list | Select-String -Pattern 'BANK_|COLLEGE_'
& wrangler.cmd secret list -c $WorkerConfig

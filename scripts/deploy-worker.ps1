<#
deploy-worker.ps1 -- deploy the breaking-monitor Worker with the real KV
namespace id, without that id living in the committed config.

WHY THIS SCRIPT EXISTS
  deploy/wrangler.toml is committed and carries a PLACEHOLDER namespace id, never
  the real one. `wrangler deploy -c deploy/wrangler.toml` therefore reaches the
  Cloudflare API and fails with `code: 10041 KV namespace not found` -- the
  placeholder is a valid 32-hex string, so nothing skips it locally and the error
  arrives as a Cloudflare rejection rather than a config error.

  This script renders the EFFECTIVE config from the committed template plus the
  real id taken from the environment, deploys that rendered file, and stamps
  WORKER_BUILD so drift-canary.yml can tell a current deployment from a stale one.

WHERE THE REAL ID COMES FROM
  $env:DMNEWS_KV_NAMESPACE_ID -- one 32-hex value, supplied per session. It is
  never committed, never written to a tracked file, and never printed by this
  script (deploy output is filtered before it reaches the screen).

DRY RUN FIRST, IF UNSURE
  It renders the config, prints the exact command it would run, and deploys
  nothing:

      $env:DMNEWS_KV_NAMESPACE_ID = '<32-hex>'   # or set it in your profile
      powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy-worker.ps1 -DryRun

WHAT IT TOUCHES ON DISK
  deploy/wrangler.local.toml -- the rendered config. GITIGNORED. Rewritten on
  every run, so it cannot go stale. It sits beside the template on purpose:
  the template's `main = "cloudflare-worker.js"` resolves relative to the config
  file's own directory.

THE BUILD STAMP
  -BuildStamp defaults to `git rev-parse --short HEAD` of this checkout, which is
  the documented convention. Pass it explicitly when the deployed content was
  synced from the public repository: drift-canary.yml resolves the stamp against
  looping-forever/daily-media-news, so a commit that exists only in the private
  working copy cannot be resolved there and the canary fails closed on it.

WHAT IT WILL NOT DO
  * deploy when the id is absent, is not 32 hex, or is still the placeholder;
  * echo the id, in any message or in wrangler's own output;
  * deploy without --var WORKER_BUILD:<sha>.
#>

[CmdletBinding()]
param(
    [string]$BuildStamp = '',
    [string]$TemplatePath = 'deploy/wrangler.toml',
    [string]$RenderedPath = 'deploy/wrangler.local.toml',
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

#: The placeholder the committed template carries. Not a secret: it is published.
$PLACEHOLDER = 'abcdefabcdefabcdefabcdefabcdefab'
$ENV_NAME = 'DMNEWS_KV_NAMESPACE_ID'

function Fail([string]$Message) {
    Write-Host ''
    Write-Host ('FAILED: ' + $Message) -ForegroundColor Red
    Write-Host 'Nothing was deployed.'
    exit 1
}

function Warn([string]$Message) {
    Write-Host ('WARNING: ' + $Message) -ForegroundColor Yellow
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    # --- the template --------------------------------------------------------
    if (-not (Test-Path -LiteralPath $TemplatePath)) {
        Fail ('no template at ' + (Join-Path $root $TemplatePath) +
              '. Run this from a checkout that holds deploy\wrangler.toml.')
    }

    # --- the real namespace id, from the environment -------------------------
    $kv = ''
    if ($null -ne $env:DMNEWS_KV_NAMESPACE_ID) { $kv = $env:DMNEWS_KV_NAMESPACE_ID.Trim() }

    if ($kv.Length -eq 0) {
        Write-Host 'FAILED: the KV namespace id is not supplied.' -ForegroundColor Red
        Write-Host ''
        Write-Host ('Set ' + $ENV_NAME + ' for this session first. The id is a 32-hex')
        Write-Host 'string, listed by:'
        Write-Host '    wrangler.cmd kv namespace list'
        Write-Host ''
        Write-Host 'Then, in the SAME session:'
        Write-Host ('    $env:' + $ENV_NAME + ' = ''<the 32-hex id>''')
        Write-Host '    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy-worker.ps1'
        Write-Host ''
        Write-Host 'There is deliberately no fallback. The committed config carries a'
        Write-Host 'placeholder, and deploying it fails with code 10041'
        Write-Host '(KV namespace not found) instead of binding the real namespace.'
        exit 2
    }

    if ($kv -notmatch '^[0-9a-fA-F]{32}$') {
        Fail ($ENV_NAME + ' is not 32 hex characters. Its value is not shown here. ' +
              'Use the `id` column from: wrangler.cmd kv namespace list')
    }
    if ($kv -eq $PLACEHOLDER) {
        Fail ($ENV_NAME + ' is set to the committed PLACEHOLDER id, not the real one. ' +
              'Deploying it would fail with code 10041 (KV namespace not found).')
    }

    # --- the build stamp -----------------------------------------------------
    $sha = $BuildStamp.Trim()
    if ($sha.Length -eq 0) {
        $sha = (& git rev-parse --short HEAD 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $sha.Length -eq 0) {
            Fail 'no -BuildStamp given and git could not resolve HEAD in this checkout.'
        }
    }
    if ($sha -notmatch '^[0-9a-fA-F]{7,40}$') {
        Fail ('the build stamp ' + $sha + ' is not a commit sha (7 or more hex digits). ' +
              'A branch or tag name is not proof of which commit was deployed.')
    }

    # --- render the effective config ----------------------------------------
    # Substituting the id into a TEMP string and writing a file wrangler reads is
    # the whole mechanism. No TOML is parsed here: the template is the source of
    # truth for everything except one value.
    $template = Get-Content -LiteralPath $TemplatePath -Raw
    $occurrences = ([regex]::Matches($template, [regex]::Escape($PLACEHOLDER))).Count
    if ($occurrences -ne 1) {
        Fail ('expected exactly one placeholder id in ' + $TemplatePath + ', found ' +
              $occurrences + '. The template and this script disagree about its shape, ' +
              'so the effective config would be wrong.')
    }
    $rendered = $template.Replace($PLACEHOLDER, $kv)

    # Byte-order mark copied from the template rather than assumed: a BOM that
    # appears or disappears changes what the TOML parsers on both sides see.
    $head = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $TemplatePath))
    $hasBom = ($head.Length -ge 3 -and $head[0] -eq 0xEF -and $head[1] -eq 0xBB -and $head[2] -eq 0xBF)
    $absoluteRendered = Join-Path $root $RenderedPath
    [IO.File]::WriteAllText($absoluteRendered, $rendered, (New-Object Text.UTF8Encoding($hasBom)))

    Write-Host ('Template: ' + $TemplatePath + '  (placeholder id only)')
    Write-Host ('Rendered: ' + $RenderedPath + '  (gitignored, real id written there)')
    Write-Host ('Namespace id: the real one, supplied via ' + $ENV_NAME + ' -- not shown here.')
    Write-Host ('Build stamp: ' + $sha)

    $wrangler = Get-Command wrangler.cmd -ErrorAction SilentlyContinue
    if (-not $wrangler) {
        Fail 'wrangler.cmd is not on PATH. Install it with: npm install -g wrangler'
    }

    $deployArgs = @('deploy', '-c', $RenderedPath, '--var', ('WORKER_BUILD:' + $sha))

    if ($DryRun) {
        Write-Host ''
        Write-Host ('DRY RUN -- would run: wrangler.cmd ' + ($deployArgs -join ' '))
        Write-Host 'The rendered config is on disk; nothing was deployed.'
        exit 0
    }

    # --- deploy --------------------------------------------------------------
    # Output goes through a redaction filter: wrangler prints binding details, and
    # the namespace id must not reach the screen or a transcript even by accident.
    Write-Host ''
    $idPattern = [regex]::Escape($kv)
    $lines = New-Object System.Collections.Generic.List[string]
    & $wrangler.Source @deployArgs 2>&1 | ForEach-Object {
        $safe = ($_.ToString() -replace $idPattern, '<kv-namespace-id>')
        $lines.Add($safe)
        Write-Host $safe
    }
    $rc = $LASTEXITCODE

    if ($rc -ne 0) {
        Write-Host ''
        Write-Host ('FAILED: wrangler exited ' + $rc + '. The previous version is still live.') -ForegroundColor Red
        Write-Host ('The rendered config is at ' + $RenderedPath + ' -- re-run with -DryRun to inspect the command.')
        exit 1
    }

    # --- report what actually shipped ---------------------------------------
    $versionId = ''
    foreach ($line in $lines) {
        $m = [regex]::Match($line, '(?:Current Version ID|Version ID)\s*:?\s*([0-9a-fA-F-]{36})')
        if ($m.Success -and $versionId.Length -eq 0) { $versionId = $m.Groups[1].Value }
    }

    Write-Host ''
    if ($versionId.Length -gt 0) {
        Write-Host ('Version id:  ' + $versionId) -ForegroundColor Green
    }
    else {
        Write-Host 'Version id:  not parsed from the deploy output above. Check with:' -ForegroundColor Yellow
        Write-Host ('    wrangler.cmd versions list -c ' + $RenderedPath)
    }
    Write-Host ('Build var:   WORKER_BUILD:' + $sha) -ForegroundColor Green

    # --- verify from the outside -------------------------------------------
    # The deployment is not evidence that it took effect. The health endpoint is:
    # it reports the WORKER_BUILD the RUNNING code sees.
    $schedulePath = 'deploy/schedule.json'
    if (-not (Test-Path -LiteralPath $schedulePath)) {
        Warn ('no ' + $schedulePath + ', so the health check was skipped. This deploy is NOT verified.')
        exit 0
    }
    $healthUrl = (Get-Content -LiteralPath $schedulePath -Raw | ConvertFrom-Json).health_url
    if (-not $healthUrl) {
        Warn ('no health_url in ' + $schedulePath + ', so the health check was skipped. This deploy is NOT verified.')
        exit 0
    }

    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 25
    }
    catch {
        Write-Host ''
        Write-Host ('DEPLOYED BUT NOT VERIFIED: ' + $healthUrl + ' did not answer (' +
                    $_.Exception.Message + '). The version above is live; the build stamp ' +
                    'could not be confirmed by the Worker itself.') -ForegroundColor Red
        exit 2
    }

    $liveBuild = [string]$health.build
    Write-Host ('Health:      ' + $healthUrl)
    Write-Host ('  build = ' + $liveBuild + '   crons = ' + (($health.crons) -join ', '))
    if ($liveBuild -ne $sha) {
        Write-Host ''
        Write-Host ('DEPLOYED BUT NOT VERIFIED: the Worker reports build ''' + $liveBuild +
                    ''' but this deploy stamped ''' + $sha + '''. Either the deploy did not ' +
                    'take effect or another deployment landed after it.') -ForegroundColor Red
        exit 2
    }

    Write-Host ''
    Write-Host 'OK: deployed and verified by the Worker''s own health endpoint.' -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
}

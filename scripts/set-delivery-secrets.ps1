<#
set-delivery-secrets.ps1 -- put the digest delivery secrets on GitHub.

WHAT IT DOES. Walks the ten delivery settings one at a time, required-first,
and sets each on looping-forever/daily-media-news with `gh secret set NAME`
reading the value from STDIN. Nothing is printed but variable NAMES.

WHY THE NAMES ARE NOT WHAT YOU EXPECT. Two of the ten are ENV VAR names in the
workflow, not GitHub secret names:

  the workflow line                          env the job sees      GitHub secret
  CLOUDFLARE_API_TOKEN: ${{ secrets.CF_PAGES_API_TOKEN }}   CLOUDFLARE_API_TOKEN   CF_PAGES_API_TOKEN
  CF_LEDGER_TOKEN:      ${{ secrets.CF_PAGES_API_TOKEN }}   CF_LEDGER_TOKEN        CF_PAGES_API_TOKEN
  CF_ACCOUNT_ID:        ${{ secrets.CF_ACCOUNT_ID }}                               CF_ACCOUNT_ID

`secrets.CF_LEDGER_TOKEN` and `secrets.CLOUDFLARE_API_TOKEN` appear NOWHERE in
the repo. Setting a secret by either of those names would create a value that no
workflow reads, so this script sets CF_PAGES_API_TOKEN instead and treats the
two env names as aliases of that one secret. That is why item 2 and item 3 both
report the same underlying secret and why the second one asks nothing.

NO SECRET TOUCHES DISK, ARGV OR THE SCREEN.
  * a value already on this machine is offered with its FILE PATH only -- the
    value itself is never shown, not masked, not by length;
  * otherwise it is typed into Read-Host -AsSecureString (hidden input);
  * a blank line means SKIP, and nothing is set;
  * it is passed to gh over STDIN, never as an argument;
  * the BSTR is zeroed in `finally`;
  * no temp file is created anywhere, and no value is ever redirected.

RUN IT:

    cd <repository root>
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set-delivery-secrets.ps1

gh must be installed and authenticated with rights to set secrets on the repo.
This script refuses to start if it is not.

HONEST LIMIT. A .NET string cannot be zeroed once created, so an unwrapped copy
stays in managed memory until the GC reclaims it. The BSTR wrapper is zeroed
explicitly and the value never reaches a file, a command line or the console.
#>

[CmdletBinding()]
param(
    [string]$Repo = 'looping-forever/daily-media-news'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    Write-Host ''
    Write-Host ('FAILED: ' + $Message) -ForegroundColor Red
    exit 1
}

# Ten items. Env = the name as asked for; Secret = the GitHub secret it really
# is. Order is required-for-delivery first, cosmetic last.
$items = @(
    @{ Env = 'NTFY_TOPIC';          Secret = 'NTFY_TOPIC';          Channel = 'ntfy digest push';           Need = 'REQUIRED' },
    @{ Env = 'NTFY_SERVER';         Secret = 'NTFY_SERVER';         Channel = 'ntfy digest push';           Need = 'REQUIRED' },
    @{ Env = 'CLOUDFLARE_API_TOKEN'; Secret = 'CF_PAGES_API_TOKEN'; Channel = 'Pages archive + KV ledger';  Need = 'REQUIRED-FOR-LEDGER' },
    @{ Env = 'CF_ACCOUNT_ID';       Secret = 'CF_ACCOUNT_ID';       Channel = 'Pages archive + KV ledger';  Need = 'REQUIRED-FOR-LEDGER' },
    @{ Env = 'CF_LEDGER_TOKEN';     Secret = 'CF_PAGES_API_TOKEN'; Channel = 'KV ledger (same secret as CLOUDFLARE_API_TOKEN)'; Need = 'ALIAS' },
    @{ Env = 'GMAIL_USER';          Secret = 'GMAIL_USER';          Channel = 'Gmail backup';               Need = 'BACKUP' },
    @{ Env = 'GMAIL_APP_PASSWORD';  Secret = 'GMAIL_APP_PASSWORD';  Channel = 'Gmail backup';               Need = 'BACKUP' },
    @{ Env = 'GMAIL_RECEIVER';      Secret = 'GMAIL_RECEIVER';      Channel = 'Gmail backup (blank: falls back to GMAIL_USER)'; Need = 'OPTIONAL' },
    @{ Env = 'TELEGRAM_BOT_TOKEN';  Secret = 'TELEGRAM_BOT_TOKEN';  Channel = 'Telegram breaking alerts';   Need = 'OPTIONAL' },
    @{ Env = 'TELEGRAM_CHAT_ID';    Secret = 'TELEGRAM_CHAT_ID';    Channel = 'Telegram (blank: auto-discovered)'; Need = 'OPTIONAL' }
)

# --- guard: gh present, gh authenticated, repo reachable ---------------------
# All three run BEFORE anything is collected, so a bad environment costs no typing.
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Fail 'gh (GitHub CLI) is not on PATH, so no secret can be set from here.'
}

$here = (Get-Location).Path
Write-Host ''
Write-Host ('repo   : ' + $Repo)
Write-Host ('running: ' + $here)

$ErrorActionPreference = 'Continue'
$null = & gh auth status 2>&1
$authRc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($authRc -ne 0) {
    Fail 'gh is not authenticated (gh auth status failed). Run `gh auth login` first.'
}

$ErrorActionPreference = 'Continue'
$null = & gh repo view $Repo --json name 2>&1
$repoRc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($repoRc -ne 0) {
    Fail ('the repository ' + $Repo + ' does not exist, or this account cannot see it.')
}
Write-Host 'gh is authenticated and the repository exists.'
Write-Host ''
Write-Host 'Ten settings. Enter a blank line at any prompt to SKIP that one.'
Write-Host 'The value is never displayed.'

# --- local lookup: returns @{ Value; Source } or $null ----------------------
# Source is always a PATH or the words "environment variable NAME". The value
# is returned to the caller and never written to the console by this function.
function Get-LocalCandidate([string]$envName, [string]$secretName) {
    $envNames = @($envName, $secretName)
    if ($secretName -eq 'CF_PAGES_API_TOKEN') { $envNames += 'CLOUDFLARE_API_TOKEN' }
    foreach ($n in $envNames) {
        $v = [System.Environment]::GetEnvironmentVariable($n)
        if ($v -and $v.Trim().Length -gt 0) {
            return @{ Value = $v; Source = ('environment variable ' + $n) }
        }
    }

    $wrangler = Join-Path $here 'deploy\wrangler.toml'
    if ($envName -eq 'NTFY_SERVER' -and (Test-Path -LiteralPath $wrangler)) {
        $m = Select-String -LiteralPath $wrangler -Pattern '^\s*NTFY_SERVER\s*=\s*"([^"]+)"' |
             Select-Object -First 1
        if ($null -ne $m) {
            return @{ Value = $m.Matches[0].Groups[1].Value; Source = $wrangler }
        }
    }

    if ($envName -eq 'CF_ACCOUNT_ID') {
        $acct = Join-Path $here 'deploy\.wrangler\cache\wrangler-account.json'
        if (Test-Path -LiteralPath $acct) {
            try {
                $obj = Get-Content -LiteralPath $acct -Raw | ConvertFrom-Json
                $id = [string]$obj.account.id
                if ($id.Length -gt 0) { return @{ Value = $id; Source = $acct } }
            }
            catch { }
        }
    }
    return $null
}

$result = @{}
$handled = @{}
$index = 0

foreach ($item in $items) {
    $index++
    $envName = $item.Env
    $secretName = $item.Secret

    Write-Host ''
    Write-Host ('[' + $index + '/' + $items.Count + '] ' + $envName) -ForegroundColor Cyan
    Write-Host ('    ' + $item.Need + ' -- ' + $item.Channel)

    # Two of the ten are aliases of one secret. Do not prompt twice for it.
    if ($handled.ContainsKey($secretName)) {
        $prev = $handled[$secretName]
        $result[$envName] = $prev
        Write-Host ('    same GitHub secret as an earlier item (' + $secretName + '), already ' + $prev + '.') -ForegroundColor DarkGray
        continue
    }

    $secure = $null
    $candidate = Get-LocalCandidate -envName $envName -secretName $secretName

    if ($null -ne $candidate) {
        Write-Host ('    a value for this exists at: ' + $candidate.Source) -ForegroundColor Yellow
        Write-Host '    Use it? Y = use it, N = type a different one, blank = skip.'
        $answer = Read-Host '    [Y/N/blank]'
        if ($answer -ceq 'Y' -or $answer -ceq 'y') {
            $secure = ConvertTo-SecureString -String $candidate.Value -AsPlainText -Force
            Write-Host ('    using the value from ' + $candidate.Source) -ForegroundColor DarkGray
        }
        elseif ($answer -ceq 'N' -or $answer -ceq 'n') {
            $secure = $null
        }
        else {
            Write-Host '    skipped.'
            $result[$envName] = 'MISSING'
            continue
        }
        $candidate = $null
    }

    if ($null -eq $secure) {
        Write-Host '    type the value (hidden), then press Enter. Blank = skip.'
        $secure = Read-Host -AsSecureString -Prompt ('    ' + $envName)
    }

    if ($null -eq $secure -or $secure.Length -eq 0) {
        Write-Host '    skipped.'
        $result[$envName] = 'MISSING'
        $secure = $null
        continue
    }

    # --- unwrap, send over STDIN, zero the wrapper --------------------------
    $bstr = [IntPtr]::Zero
    $plain = $null
    $rc = 1
    $err = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

        # STDIN, never argv: without --body, `gh secret set NAME` reads the value
        # from standard input. --body would expose it in the process command line.
        $ErrorActionPreference = 'Continue'
        $plain | & gh secret set $secretName --repo $Repo
        $rc = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    }
    catch {
        $err = $_.Exception.Message
        $rc = 1
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        $plain = $null
        $secure = $null
        Remove-Variable -Name plain -ErrorAction SilentlyContinue
        Remove-Variable -Name secure -ErrorAction SilentlyContinue
        $bstr = [IntPtr]::Zero
        $ErrorActionPreference = 'Stop'
    }

    if ($rc -ne 0) {
        if ($null -ne $err) { Write-Host ('    gh error: ' + $err) -ForegroundColor Red }
        Write-Host ('    FAILED to set ' + $secretName + ' (gh exit ' + $rc + ')') -ForegroundColor Red
        $result[$envName] = 'MISSING'
        continue
    }

    Write-Host ('    SET ' + $secretName) -ForegroundColor Green
    $result[$envName] = 'SET'
    $handled[$secretName] = 'SET'
}

# --- verify by NAME only, then report ---------------------------------------
# `gh secret list` returns names and timestamps, never values.
Write-Host ''
$ErrorActionPreference = 'Continue'
$listed = & gh secret list --repo $Repo 2>&1
$listRc = $LASTEXITCODE
$ErrorActionPreference = 'Stop'

$listedText = ''
if ($listRc -eq 0) { $listedText = ($listed | Out-String) }

Write-Host '================ RESULT (names only) ================'
foreach ($item in $items) {
    $name = $item.Env
    $state = $result[$name]
    if ($null -eq $state) { $state = 'MISSING' }

    $confirmed = $state
    if ($listRc -eq 0) {
        $found = $listedText -match ('(?m)^' + [regex]::Escape($item.Secret) + '\s')
        if ($found -and $state -eq 'SET') { $confirmed = 'SET' }
        elseif ($found -and $state -eq 'MISSING') { $confirmed = 'SET (already present)' }
        elseif (-not $found) { $confirmed = 'MISSING' }
    }

    $colour = 'Red'
    if ($confirmed -like 'SET*') { $colour = 'Green' }
    Write-Host ('  {0,-22} {1,-22} {2}' -f $name, $confirmed, $item.Secret) -ForegroundColor $colour
}

Write-Host '====================================================='
Write-Host 'Column 1 is the name you asked for, column 3 is the GitHub secret it maps to.'
Write-Host 'No value was printed, and no value was written to disk.'
Write-Host ''
Write-Host 'To prove delivery after the next run:'
Write-Host '  https://breaking-monitor.<subdomain>.workers.dev/?health=1   -> digest.last_delivery'
Write-Host 'If ntfy arrives but last_delivery stays stale, CF_PAGES_API_TOKEN'
Write-Host 'lacks Account > Workers KV Storage > Edit.'
exit 0

# Sets the two Pages secrets the digest archive's Basic auth compares against.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/set-archive-password.ps1
#
# Values are typed into a hidden prompt and piped straight to wrangler. Nothing
# is written to disk, echoed, or committed; only the NAMES are printed back.
# Safe to re-run: both calls overwrite the existing secret.
#
# The username is a secret for the same reason the password is: the middleware
# compares against whatever `DIGEST_USERNAME` holds, and a literal in
# functions/_middleware.js is a value the whole world can read.

$ErrorActionPreference = 'Stop'
$ProjectName = 'daily-digest'

function Read-Hidden([string]$Prompt) {
    $secure = Read-Host -AsSecureString $Prompt
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

$username = Read-Hidden 'Archive login username (hidden)'
if ([string]::IsNullOrWhiteSpace($username)) { throw 'empty username: nothing was set' }
if ($username.Contains(':')) {
    # Basic auth splits the header on the FIRST colon, so a colon here cannot be
    # escaped and would arrive truncated.
    throw 'the username must not contain a colon'
}

$password = Read-Hidden 'Archive login password (hidden)'
if ([string]::IsNullOrWhiteSpace($password)) { throw 'empty password: nothing was set' }

try {
    $username | & wrangler.cmd pages secret put DIGEST_USERNAME --project-name $ProjectName
    if ($LASTEXITCODE -ne 0) { throw 'wrangler pages secret put DIGEST_USERNAME failed' }

    $password | & wrangler.cmd pages secret put DIGEST_PASSWORD --project-name $ProjectName
    if ($LASTEXITCODE -ne 0) { throw 'wrangler pages secret put DIGEST_PASSWORD failed' }
} finally {
    $password = $null
}

Write-Host ''
Write-Host "Set on Pages project '$ProjectName': DIGEST_USERNAME, DIGEST_PASSWORD"
& wrangler.cmd pages secret list --project-name $ProjectName

"""`scripts/set-private-config.ps1` masks its input and never prints a value.

WHY THIS FILE EXISTS
--------------------
That script is the only thing that puts the deployment's private proper nouns
(BANK_NAME / BANK_URL / COLLEGE_NAME / COLLEGE_URL) and the archive origin into
GitHub and Cloudflare. It printed nothing on any observed run, which is exactly
why nothing checked it: the failure mode is a *leak*, and a leak leaves the run
looking normal. Three lines in it carry that promise and each is one edit away
from breaking it:

  * `Read-Host -AsSecureString` -- dropping `-AsSecureString` echoes the typed
    value to the console and into whatever transcript the run produces;
  * the name-only `Write-Host` lines -- a "helpful" `Write-Host "set $Name=$value"`
    publishes the secret;
  * `gh variable list | Select-String` -- `gh variable list` prints a VALUE
    column, so piping it straight through publishes the archive origin. The
    comment at the bottom of the script records that this actually happened.

So the tests below are about the promise, not the plumbing: input is masked, no
value reaches an output stream, and all four institution names plus the archive
origin are actually handed to the tool that stores them.

HOW IT DECIDES
--------------
Two arms.

1. STATIC (runs everywhere, including CI). It parses the script with PowerShell's
   own parser and inspects the AST. That is deliberate: a text search cannot tell
   a value inside a single-quoted string from a value inside an expandable one,
   and "it never echoes a value" is precisely that distinction. Every `Read-Host`
   must carry `-AsSecureString`, and no output-producing command may have a
   reference to the two variables that hold a value (`$value`, `$origin`)
   anywhere inside its arguments.

2. BEHAVIOURAL (Windows only). It runs the real script against a stubbed `gh` and
   `wrangler.cmd` placed first on PATH, with `Read-Host` shadowed so no human is
   needed. The stubs record the argv and the piped stdin, so the test can prove a
   value arrives on STDIN and never in argv, and the script's own stdout is
   checked for every value. No real `gh` or `wrangler` is reachable, so no real
   variable or secret can be written.

   `wrangler.cmd` is a Windows-only invocation, so this arm reports a skip on
   POSIX rather than pretending it ran. The static arm still covers CI.

WHAT IT CANNOT DO
-----------------
It cannot prove GitHub or Cloudflare stored anything -- the stubs replace both.
It also cannot catch a leak through a channel that is not an output command (for
example a native tool the script itself calls that echoes its input); it asserts
what this script does, which is the part that regressed-class of change lives in.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_REL = "scripts/set-private-config.ps1"

#: The four names the script exists to set. They are stored as GitHub Actions
#: SECRETS (read as env vars by scripts/private_values.py); the archive origin is
#: the one that is a repository VARIABLE. The distinction is load-bearing:
#: `gh variable list` prints values and `gh secret list` does not.
INSTITUTION_NAMES = ("BANK_NAME", "BANK_URL", "COLLEGE_NAME", "COLLEGE_URL")
ARCHIVE_VARIABLE = "CF_ARCHIVE_ORIGIN"
WORKER_SECRET = "DIGEST_ARCHIVE_ORIGIN"

#: The values the behavioural harness types in, in TRIMMED form. RUN_HARNESS
#: supplies the origin with a trailing slash on purpose; the trimmed spelling here
#: is what proves the trim, and it is a substring of the untrimmed one, so an argv
#: scan for it catches both spellings.
ARCHIVE_ORIGIN_VALUE = "https://example.pages.dev"
INSTITUTION_VALUES = (
    "Test Bank (Branch)",
    "https://bank.example.com",
    "Test Campus",
    "https://college.example.com",
)

#: The variables that hold a value between being read and being stored. A
#: reference to either inside an output command is the leak this file guards.
VALUE_VARIABLES = ("value", "origin")

#: Commands that put text where a human, a log, or a transcript can read it.
OUTPUT_COMMANDS = (
    "write-host", "write-output", "write-verbose", "write-information",
    "write-warning", "write-error", "write-debug", "out-host", "out-string",
    "echo",
)

#: The AST dump. ASCII only: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI,
#: so a stray non-ASCII character here would be a parse error rather than a test.
AST_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$scriptPath = __SCRIPT__
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath, [ref]$tokens, [ref]$errors)
if ($errors -and $errors.Count -gt 0) {
    foreach ($e in $errors) {
        Write-Output ('PARSE_ERROR line ' + $e.Extent.StartLineNumber + ': ' + $e.Message)
    }
    exit 3
}
$commands = New-Object System.Collections.ArrayList
foreach ($cmdAst in $ast.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $argText = New-Object System.Collections.ArrayList
    $varNames = New-Object System.Collections.ArrayList
    foreach ($element in $cmdAst.CommandElements) {
        [void]$argText.Add($element.Extent.Text)
        foreach ($varAst in $element.FindAll(
                { param($node) $node -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
            [void]$varNames.Add($varAst.VariablePath.UserPath)
        }
    }
    $captured = $false
    $parent = $cmdAst.Parent
    $depth = 0
    while ($parent -ne $null -and $depth -lt 4) {
        if ($parent -is [System.Management.Automation.Language.AssignmentStatementAst]) {
            $captured = $true
            break
        }
        $parent = $parent.Parent
        $depth = $depth + 1
    }
    [void]$commands.Add([ordered]@{
        name = [string]$cmdAst.GetCommandName()
        args = @($argText)
        vars = @($varNames)
        captured = $captured
        line = $cmdAst.Extent.StartLineNumber
    })
}
$strings = New-Object System.Collections.ArrayList
foreach ($s in $ast.FindAll(
        { param($node) $node -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
    [void]$strings.Add([string]$s.Value)
}
[ordered]@{ commands = @($commands); strings = @($strings) } |
    ConvertTo-Json -Depth 5 -Compress
"""

#: The behavioural harness. `Read-Host` is shadowed in this scope, and a function
#: defined here is visible to the script `&`-invoked below, so the real script's
#: own `Read-Host -AsSecureString` call lands here instead of on a console.
RUN_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$env:PATH = __STUBDIR__ + ';' + $env:PATH
$env:DMNEWS_TEST_LOG = __LOGFILE__
$env:DMNEWS_TEST_STDIN = __STDINFILE__
$global:DMNEWS_TEST_VALUES = New-Object System.Collections.Queue
@(
    'https://example.pages.dev/',
    'Test Bank (Branch)',
    'https://bank.example.com',
    'Test Campus',
    'https://college.example.com'
) | ForEach-Object { $global:DMNEWS_TEST_VALUES.Enqueue($_) }
function Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    if (-not $AsSecureString) {
        throw 'Read-Host was called without -AsSecureString'
    }
    if ($global:DMNEWS_TEST_VALUES.Count -eq 0) {
        throw 'the script asked for more values than this test supplies'
    }
    $v = $global:DMNEWS_TEST_VALUES.Dequeue()
    return (ConvertTo-SecureString -String $v -AsPlainText -Force)
}
& __SCRIPT__
"""

#: The stubs are batch shims on purpose. `wrangler secret put` is called with
#: `-c`, and a PowerShell script with a parameter block would try to bind `-c` as
#: a parameter name. Batch `%*` copies argv verbatim; the one PowerShell helper
#: only handles the two things batch cannot do cleanly -- printing names and
#: capturing piped stdin.
GH_CMD = r"""@echo off
>>"%DMNEWS_TEST_LOG%" echo gh %*
set "DMNEWS_STUB_MODE=none"
if /i "%1 %2"=="secret set" set "DMNEWS_STUB_MODE=stdin:secret set %3"
if /i "%1 %2"=="variable set" set "DMNEWS_STUB_MODE=stdin:variable set %3"
if /i "%1 %2"=="variable list" set "DMNEWS_STUB_MODE=print:CF_ARCHIVE_ORIGIN"
if /i "%1 %2"=="secret list" set "DMNEWS_STUB_MODE=print:BANK_NAME,BANK_URL,COLLEGE_NAME,COLLEGE_URL"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stub.ps1"
exit /b %ERRORLEVEL%
"""

WRANGLER_CMD = r"""@echo off
>>"%DMNEWS_TEST_LOG%" echo wrangler %*
set "DMNEWS_STUB_MODE=none"
if /i "%1 %2"=="secret put" if not exist "%~5" (>>"%DMNEWS_TEST_LOG%" echo MISSING_CONFIG %~5 & exit /b 1)
if /i "%1 %2"=="secret put" if exist "%~5" set "DMNEWS_STUB_MODE=stdin:secret put %3"
if /i "%1 %2"=="secret list" set "DMNEWS_STUB_MODE=print:DIGEST_ARCHIVE_ORIGIN"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stub.ps1"
exit /b %ERRORLEVEL%
"""

STUB_PS1 = r"""$mode = [string]$env:DMNEWS_STUB_MODE
if ($mode.StartsWith('print:')) {
    $mode.Substring(6).Split(',') | ForEach-Object { $_ }
} elseif ($mode.StartsWith('stdin:')) {
    $payload = [Console]::In.ReadToEnd()
    Add-Content -LiteralPath $env:DMNEWS_TEST_STDIN -Value ($mode.Substring(6) + ' <= ' + $payload.Trim())
}
exit 0
"""


def _powershell():
    """The PowerShell executable, or None. `pwsh` first so CI's runner works."""
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _ps_literal(value):
    """A PowerShell single-quoted literal -- backslashes stay literal."""
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(exe, script_text, cwd, env=None, timeout=240):
    """Run one harness file; returns the CompletedProcess with bytes intact."""
    with tempfile.TemporaryDirectory(prefix="dmnews-ps-") as tmp:
        path = os.path.join(tmp, "harness.ps1")
        with open(path, "w", encoding="ascii", newline="\r\n") as handle:
            handle.write(script_text)
        argv = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                "Bypass", "-File", path]
        return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout,
                              env=env)


def _decode(raw):
    return (raw or b"").decode("utf-8", "replace")


class StaticParseTest(unittest.TestCase):
    """Arm 1: the AST says the input is masked and no value is ever printed."""

    @classmethod
    def setUpClass(cls):
        cls.script_path = os.path.join(REPO_ROOT, SCRIPT_REL)
        if not os.path.isfile(cls.script_path):
            raise unittest.SkipTest("%s does not exist" % SCRIPT_REL)
        cls.exe = _powershell()
        if cls.exe is None:
            raise unittest.SkipTest(
                "no PowerShell on PATH, so the script cannot be parsed. This arm "
                "runs on the Windows host and on GitHub's ubuntu runner (pwsh).")
        harness = AST_HARNESS.replace("__SCRIPT__", _ps_literal(cls.script_path))
        done = _run_powershell(cls.exe, harness, REPO_ROOT)
        out = _decode(done.stdout).strip()
        err = _decode(done.stderr).strip()
        if done.returncode != 0 or out.startswith("PARSE_ERROR"):
            raise AssertionError(
                "the AST harness could not read %s (exit %s):\n%s\n%s"
                % (SCRIPT_REL, done.returncode, out or "(no stdout)", err))
        try:
            cls.payload = json.loads(out)
        except ValueError as exc:
            raise AssertionError(
                "the AST harness did not return JSON (%s):\n%s" % (exc, out or err))
        cls.commands = cls.payload.get("commands") or []
        cls.strings = cls.payload.get("strings") or []

    def _named(self, name):
        wanted = name.lower()
        return [c for c in self.commands
                if (c.get("name") or "").lower() == wanted]

    def test_every_read_host_is_masked(self):
        """`Read-Host` without `-AsSecureString` echoes what is typed."""
        read_host = self._named("Read-Host")
        self.assertTrue(
            read_host,
            "%s no longer calls Read-Host. That is fine only if it reads input "
            "some other masked way; this test pins the mask, so update it with "
            "the new mechanism rather than deleting it." % SCRIPT_REL)
        unmasked = [c for c in read_host if "-AsSecureString" not in c.get("args", [])]
        self.assertEqual(
            [], unmasked,
            "these Read-Host calls do not pass -AsSecureString, so the typed "
            "value is echoed to the console and any transcript (%s):\n  %s"
            % (SCRIPT_REL,
               "\n  ".join("line %s: %s" % (c.get("line"), " ".join(c.get("args", [])))
                           for c in unmasked)))

    def test_no_output_command_references_a_value(self):
        """No Write-Host / Write-Output / echo ... may mention $value or $origin."""
        offenders = []
        for command in self.commands:
            if (command.get("name") or "").lower() not in OUTPUT_COMMANDS:
                continue
            touched = [v for v in (command.get("vars") or [])
                       if v.lower() in VALUE_VARIABLES]
            if touched:
                offenders.append(
                    "line %s: %s -> %s"
                    % (command.get("line"),
                       " ".join(command.get("args") or []),
                       ", ".join("$" + v for v in touched)))
        self.assertEqual(
            [], offenders,
            "these output commands reference a variable that holds a private "
            "value, so the value reaches the console or the run log. Print the "
            "NAME only (%s):\n  %s" % (SCRIPT_REL, "\n  ".join(offenders)))

    def test_the_variable_listing_is_captured_not_printed(self):
        """`gh variable list` prints a VALUE column, so it must be captured.

        The script reads it into `$repoVariables` and prints only whether the
        name appeared. A bare `& gh variable list` writes the value straight to
        the console -- the regression recorded in the comment at the foot of the
        script, and the reason it exists.
        """
        offenders = []
        for command in self._named("gh"):
            args = command.get("args") or []
            if "variable" in args and "list" in args and not command.get("captured"):
                offenders.append("line %s: %s"
                                 % (command.get("line"), " ".join(args)))
        self.assertEqual(
            [], offenders,
            "these `gh variable list` calls print to the console instead of being "
            "captured. `gh variable list` prints a VALUE column, so this publishes "
            "the archive origin (%s):\n  %s"
            % (SCRIPT_REL, "\n  ".join(offenders)))

    def test_all_four_institution_names_are_declared(self):
        missing = [name for name in INSTITUTION_NAMES if name not in self.strings]
        self.assertEqual(
            [], missing,
            "%s no longer declares %s, so that value is never stored and the "
            "digest silently renders a placeholder name."
            % (SCRIPT_REL, ", ".join(missing) or "-"))

    def test_the_four_names_are_routed_to_gh_secret_set(self):
        """A name that is declared but never passed on is a config that promises."""
        calls = [c for c in self._named("Set-RepoSecret")]
        self.assertTrue(
            calls,
            "%s no longer calls Set-RepoSecret, so the institution values are "
            "read and then thrown away." % SCRIPT_REL)
        secret_sets = [c for c in self._named("gh")
                       if "secret" in (c.get("args") or [])
                       and "set" in (c.get("args") or [])]
        self.assertTrue(
            secret_sets,
            "%s has no `gh secret set` call, so no institution value is stored."
            % SCRIPT_REL)
        # The value must arrive on stdin or in an argument of this call, and the
        # name must be the loop's spec -- never a second, hand-written list.
        self.assertTrue(
            any(any(v.lower() == "value" for v in (c.get("vars") or []))
                for c in calls),
            "Set-RepoSecret is no longer handed the read $value, so the mask and "
            "the store have been disconnected in %s" % SCRIPT_REL)

    def test_the_archive_origin_is_a_variable_and_a_worker_secret(self):
        """It honestly lives twice; losing either half breaks the seal."""
        variable_sets = [c for c in self._named("gh")
                         if "variable" in (c.get("args") or [])
                         and "set" in (c.get("args") or [])
                         and ARCHIVE_VARIABLE in (c.get("args") or [])]
        self.assertTrue(
            variable_sets,
            "%s no longer runs `gh variable set %s`, so scripts/deploy-worker.ps1 "
            "cannot read the archive origin." % (SCRIPT_REL, ARCHIVE_VARIABLE))
        worker_sets = [c for c in self._named("wrangler.cmd")
                       if "secret" in (c.get("args") or [])
                       and "put" in (c.get("args") or [])
                       and WORKER_SECRET in (c.get("args") or [])]
        self.assertTrue(
            worker_sets,
            "%s no longer runs `wrangler.cmd secret put %s`, so the Worker cannot "
            "validate ingest against the origin." % (SCRIPT_REL, WORKER_SECRET))


@unittest.skipUnless(os.name == "nt",
                     "the script invokes `wrangler.cmd`, so the behavioural arm "
                     "is Windows-only; the static arm still covers CI")
class StubbedProvidersTest(unittest.TestCase):
    """Arm 2: run the real script against fake `gh` and `wrangler.cmd`.

    The stubs are first on PATH, so a real CLI cannot be reached and no real
    variable or secret can be written by this test.
    """

    @classmethod
    def setUpClass(cls):
        cls.script_path = os.path.join(REPO_ROOT, SCRIPT_REL)
        if not os.path.isfile(cls.script_path):
            raise unittest.SkipTest("%s does not exist" % SCRIPT_REL)
        cls.exe = _powershell()
        if cls.exe is None:
            raise unittest.SkipTest("no PowerShell on PATH")

        cls.tmp = tempfile.mkdtemp(prefix="dmnews-stub-")
        cls.stub_dir = os.path.join(cls.tmp, "bin")
        os.makedirs(cls.stub_dir)
        cls.log_path = os.path.join(cls.tmp, "calls.log")
        cls.stdin_path = os.path.join(cls.tmp, "stdin.log")
        for path in (cls.log_path, cls.stdin_path):
            with open(path, "w", encoding="ascii") as handle:
                handle.write("")

        for name, body in (("gh", GH_CMD), ("wrangler", WRANGLER_CMD)):
            with open(os.path.join(cls.stub_dir, "%s.cmd" % name), "w",
                      encoding="ascii", newline="\r\n") as handle:
                handle.write(body)
        with open(os.path.join(cls.stub_dir, "stub.ps1"), "w",
                  encoding="ascii", newline="\r\n") as handle:
            handle.write(STUB_PS1)

        harness = (RUN_HARNESS
                   .replace("__STUBDIR__", _ps_literal(cls.stub_dir))
                   .replace("__LOGFILE__", _ps_literal(cls.log_path))
                   .replace("__STDINFILE__", _ps_literal(cls.stdin_path))
                   .replace("__SCRIPT__", _ps_literal(cls.script_path)))
        cls.done = _run_powershell(cls.exe, harness, REPO_ROOT)
        cls.stdout = _decode(cls.done.stdout)
        cls.stderr = _decode(cls.done.stderr)
        with open(cls.log_path, encoding="ascii") as handle:
            cls.calls = [line.strip() for line in handle if line.strip()]
        with open(cls.stdin_path, encoding="ascii") as handle:
            cls.stdin_lines = [line.strip() for line in handle if line.strip()]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    def test_the_script_ran_against_the_stubs(self):
        self.assertEqual(
            0, self.done.returncode,
            "the script failed against the stubbed providers (exit %s):\n%s\n%s"
            % (self.done.returncode, self.stdout, self.stderr))
        self.assertTrue(
            self.calls,
            "no stub was called, so the script was not exercised at all:\n%s"
            % self.stdout)

    def test_every_institution_value_arrives_on_stdin_not_in_argv(self):
        """`gh secret set` takes the value on stdin; argv is world-readable."""
        expected = {
            "BANK_NAME": "Test Bank (Branch)",
            "BANK_URL": "https://bank.example.com",
            "COLLEGE_NAME": "Test Campus",
            "COLLEGE_URL": "https://college.example.com",
        }
        for name, value in expected.items():
            self.assertIn(
                "gh secret set %s" % name, self.calls,
                "the script never ran `gh secret set %s`:\n%s"
                % (name, "\n".join(self.calls)))
            self.assertIn(
                "secret set %s <= %s" % (name, value), self.stdin_lines,
                "`gh secret set %s` was not handed %r on stdin:\n%s"
                % (name, value, "\n".join(self.stdin_lines)))
            argv_lines = [line for line in self.calls
                          if value in line]
            self.assertEqual(
                [], argv_lines,
                "the value %r reached `gh` as a command-line argument, where the "
                "process table and shell history can see it:\n%s"
                % (value, "\n".join(argv_lines)))

    def test_the_archive_origin_is_trimmed_and_stored_twice(self):
        """The expectation changed DIRECTION on 2026-10-28 -- deliberately.

        This test used to require `gh variable set CF_ARCHIVE_ORIGIN --body
        <origin>` exactly, i.e. it pinned the value ONTO argv: the one place the
        rest of the script is careful never to put it, and exactly what
        `test_every_institution_value_arrives_on_stdin_not_in_argv` fails an
        institution value for. The archive variable was the hole in that rule.

        The assertion is now the opposite one and it is not narrower: the
        trailing-slash trim, both storage halves and the Worker half all stay
        pinned. What changed is which channel the value may arrive on.
        """
        origin = "https://example.pages.dev"
        # The harness supplies the value WITH a trailing slash, so the trimmed
        # form below is what proves the TrimEnd('/').
        self.assertIn(
            "gh variable set %s" % ARCHIVE_VARIABLE, self.calls,
            "the archive origin was not set as a repository variable:\n%s"
            % "\n".join(self.calls))
        self.assertIn(
            "variable set %s <= %s" % (ARCHIVE_VARIABLE, origin), self.stdin_lines,
            "the archive origin did not reach `gh variable set` on stdin, or its "
            "trailing slash was not trimmed:\n%s" % "\n".join(self.stdin_lines))
        self.assertTrue(
            any(line.startswith("wrangler secret put %s" % WORKER_SECRET)
                for line in self.calls),
            "the archive origin was not put on the Worker:\n%s"
            % "\n".join(self.calls))
        self.assertIn(
            "secret put %s <= %s" % (WORKER_SECRET, origin), self.stdin_lines,
            "the Worker secret was not handed the value on stdin:\n%s"
            % "\n".join(self.stdin_lines))

    def test_no_value_reaches_any_provider_argv(self):
        """Every value this run handled is absent from EVERY recorded argv line.

        Deliberately not scoped to `gh secret set` or to the four institution
        names. The regression this catches is a call somebody ADDS later --
        `gh variable set --body`, a `wrangler ... --value`, a debug echo with the
        value spliced in -- so the scan is over every stub invocation and every
        value the harness supplied.

        Values are the TRIMMED forms, and each is a substring of its untrimmed
        original, so `https://example.pages.dev` also catches the trailing-slash
        spelling.
        """
        offenders = []
        for value in (ARCHIVE_ORIGIN_VALUE,) + INSTITUTION_VALUES:
            offenders.extend(
                "%r on argv:%s" % (value, line)
                for line in self.calls if value in line)
        self.assertEqual(
            [], offenders,
            "a value reached a provider on the command line, where the process "
            "table, the shell history and any `ps` snapshot can read it. Every "
            "value must travel on stdin:\n%s" % "\n".join(offenders))

    def _run_from(self, cwd):
        """Run the REAL script with `cwd=<cwd>` against the same stubs."""
        log = os.path.join(self.tmp, "calls-cwd.log")
        stdin_log = os.path.join(self.tmp, "stdin-cwd.log")
        for path in (log, stdin_log):
            with open(path, "w", encoding="ascii") as handle:
                handle.write("")
        harness = (RUN_HARNESS
                   .replace("__STUBDIR__", _ps_literal(self.stub_dir))
                   .replace("__LOGFILE__", _ps_literal(log))
                   .replace("__STDINFILE__", _ps_literal(stdin_log))
                   .replace("__SCRIPT__", _ps_literal(self.script_path)))
        done = _run_powershell(self.exe, harness, cwd)
        with open(log, encoding="ascii") as handle:
            calls = [line.strip() for line in handle if line.strip()]
        with open(stdin_log, encoding="ascii") as handle:
            lines = [line.strip() for line in handle if line.strip()]
        return done, calls, lines

    def test_the_script_works_from_any_directory(self):
        """It resolves the repo from ITS OWN location, not from the caller's cwd.

        Two lines here read the current directory: the `deploy/wrangler.local.toml`
        probe, and `gh`'s own repository lookup. Run from anywhere else, the probe
        used to fall through to `deploy/wrangler.toml` -- a DIFFERENT config,
        silently -- and `gh` targeted whichever repo the cwd happened to be in.

        The wrangler stub records `MISSING_CONFIG` and exits non-zero when the
        `-c` path does not exist, so the pre-fix behaviour cannot pass quietly:
        the origin never reaches the Worker. Both are asserted below.
        """
        foreign = os.path.join(self.tmp, "elsewhere")
        os.makedirs(foreign, exist_ok=True)
        done, calls, stdin_lines = self._run_from(foreign)
        self.assertEqual(
            [], [line for line in calls if line.startswith("MISSING_CONFIG")],
            "the script handed wrangler a `-c` path that does not exist, so it "
            "resolved `deploy/wrangler.*` against the CALLER's directory instead "
            "of its own (%r):\n%s" % (foreign, "\n".join(calls)))
        self.assertEqual(
            0, done.returncode,
            "the script failed when run from %r, so it still depends on the "
            "current directory:\n--- stdout ---\n%s\n--- stderr ---\n%s\n--- "
            "calls ---\n%s" % (foreign, _decode(done.stdout),
                               _decode(done.stderr), "\n".join(calls)))
        self.assertIn("gh secret set BANK_NAME", calls)
        self.assertTrue(
            any(line.startswith("wrangler secret put %s" % WORKER_SECRET)
                for line in calls),
            "the Worker secret was never reached from a foreign cwd:\n%s"
            % "\n".join(calls))
        self.assertIn(
            "secret put %s <= https://example.pages.dev" % WORKER_SECRET,
            stdin_lines,
            "the archive origin did not reach the Worker on stdin from a foreign "
            "cwd:\n%s" % "\n".join(stdin_lines))

    def test_no_value_reaches_the_script_output(self):
        """The whole point: stdout and stderr carry names, never values."""
        leaked = []
        for value in ("https://example.pages.dev", "Test Bank (Branch)",
                      "https://bank.example.com", "Test Campus",
                      "https://college.example.com"):
            if value in self.stdout or value in self.stderr:
                leaked.append(value)
        self.assertEqual(
            [], leaked,
            "the script printed %s. Every value here is a private proper noun or "
            "the archive origin; the run output must name what was set and never "
            "show what it was set to.\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (", ".join(repr(v) for v in leaked), self.stdout, self.stderr))


#: Every script that hands a value to `gh` or `wrangler`. The argv rule is a
#: CLASS rule, not a property of one line: it was broken in exactly one of these,
#: and the others were anecdote until they were parsed.
#:
#: `scripts/set-archive-password.ps1` was removed on 2026-10-30 with the
#: credential gate it configured (the digest archive is public by design).
SECRET_SCRIPTS = (
    "scripts/set-private-config.ps1",
    "scripts/set-delivery-secrets.ps1",
)

#: Variables that hold a value between being read and being stored.
VALUE_BEARING = ("value", "origin", "plain", "password", "username", "secure")


def _stores_a_value(command):
    """Is this command one that writes a secret or variable to a provider?"""
    name = (command.get("name") or "").lower()
    args = [str(a).lower() for a in (command.get("args") or [])]
    if name == "gh":
        return (("secret" in args and "set" in args)
                or ("variable" in args and "set" in args))
    if name in ("wrangler", "wrangler.cmd"):
        return "secret" in args and "put" in args
    return False


class NoValueOnProviderArgvTest(unittest.TestCase):
    """Arm 3: the argv rule swept over every script that stores a secret.

    The behavioural arm pinned the bug rather than the promise: it required
    `--body <origin>`. One fixed line does not make the class safe, so this arm
    parses all three scripts and fails if a value variable or a `--body`/`-b`
    flag reaches a provider command's ARGUMENTS. A pipeline input is not an
    argument, so the correct shape passes and the leaky one does not.
    """

    @classmethod
    def setUpClass(cls):
        cls.exe = _powershell()
        if cls.exe is None:
            raise unittest.SkipTest(
                "no PowerShell on PATH, so none of the scripts can be parsed")
        cls.asts = {}
        for rel in SECRET_SCRIPTS:
            path = os.path.join(REPO_ROOT, rel)
            if not os.path.isfile(path):
                raise AssertionError(
                    "%s does not exist, so the argv rule cannot be checked there"
                    % rel)
            harness = AST_HARNESS.replace("__SCRIPT__", _ps_literal(path))
            done = _run_powershell(cls.exe, harness, REPO_ROOT)
            out = _decode(done.stdout).strip()
            if done.returncode != 0 or out.startswith("PARSE_ERROR"):
                raise AssertionError(
                    "the AST harness could not read %s (exit %s):\n%s\n%s"
                    % (rel, done.returncode, out or "(no stdout)",
                       _decode(done.stderr).strip() or "(no stderr)"))
            cls.asts[rel] = json.loads(out)

    def test_every_swept_script_actually_stores_something(self):
        """A sweep over a script that stores nothing proves nothing."""
        for rel, payload in sorted(self.asts.items()):
            found = [c for c in payload.get("commands") or []
                     if _stores_a_value(c)]
            self.assertTrue(
                found,
                "%s has no `gh secret set` / `gh variable set` / "
                "`wrangler secret put` left, so the argv sweep below is vacuous"
                % rel)

    def test_no_provider_command_takes_a_value_from_argv(self):
        offenders = []
        for rel, payload in sorted(self.asts.items()):
            for command in payload.get("commands") or []:
                if not _stores_a_value(command):
                    continue
                args = command.get("args") or []
                lowered = [str(a).lower() for a in args]
                flags = [flag for flag in ("--body", "-b") if flag in lowered]
                touched = [v for v in (command.get("vars") or [])
                           if v.lower() in VALUE_BEARING]
                if flags or touched:
                    offenders.append(
                        "%s line %s: %s%s%s"
                        % (rel, command.get("line"),
                           " ".join(str(a) for a in args),
                           ("  [flag %s]" % ", ".join(flags)) if flags else "",
                           ("  [variable %s]"
                            % ", ".join("$" + v for v in touched))
                           if touched else ""))
        self.assertEqual(
            [], offenders,
            "a value reaches a provider on the command line. `gh variable set` "
            "and `gh secret set` read the value from STDIN when --body is "
            "omitted, and argv is readable by the process table and the shell "
            "history:\n  %s" % "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)

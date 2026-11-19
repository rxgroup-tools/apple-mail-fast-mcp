"""CLI subcommands for the ``apple-mail-fast-mcp`` entry point.

Today the only subcommand is ``setup-imap`` (issue #76). The default
invocation (no subcommand) starts the MCP server — that path lives in
``server.main()`` and is unaffected by this module.
"""

from __future__ import annotations

import getpass
import os
import plistlib
import re
import subprocess
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path

from imapclient.exceptions import IMAPClientError, LoginError

from .exceptions import (
    MailAccountNotFoundError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)
from .imap_connector import ImapConnector
from .imap_overrides import (
    delete_login_override,
    delete_server_override,
    set_login_override,
    set_server_override,
)
from .imap_providers import Provider, detect_provider
from .keychain import (
    delete_imap_password,
    get_imap_password,
    set_imap_password,
)
from .mail_connector import AppleMailConnector
from .utils import is_apple_hosted_address, is_icloud_imap_host

_MAX_PASSWORD_ATTEMPTS = 3


def _print_available_accounts(accounts: list[dict[str, object]]) -> None:
    if not accounts:
        print("  (no accounts found in Mail.app)", file=sys.stderr)
        return
    for acc in accounts:
        name = acc.get("name") or "(unnamed)"
        print(f"  - {name}", file=sys.stderr)


def _account_exists(
    accounts: list[dict[str, object]], requested_name: str
) -> bool:
    return any(acc.get("name") == requested_name for acc in accounts)


def _maybe_print_icloud_login_hint(
    cli_email: str | None, host: str, email: str
) -> None:
    """On an iCloud login failure, if no `--email` was given and the login
    we tried isn't an Apple-hosted address, this is the classic #341 shape
    (third-party Apple ID, no @icloud.com alias in Mail.app). Point the user
    at the override path. No-op otherwise (incl. legitimate #201 custom-domain
    accounts, whose login succeeds and never reaches here)."""
    if (
        not cli_email
        and is_icloud_imap_host(host)
        and not is_apple_hosted_address(email)
    ):
        print(
            "  Hint: this looks like an iCloud account whose Apple ID is "
            "a third-party email. Re-run with `--email <your "
            "@icloud.com/@me.com address>` to set the correct IMAP login.",
            file=sys.stderr,
        )


def _offer_app_password_page(
    provider: Provider,
    *,
    open_url_fn: Callable[[str], object],
    input_fn: Callable[[str], str],
) -> None:
    """Print scoped-credential guidance for the detected provider and, when it
    has an app-password page, offer to open it in the browser. (#384)"""
    print(
        "\nThis uses a scoped app-specific password — limited to this one "
        "account and revocable anytime (unlike granting full disk access)."
    )
    print(f"\nProvider detected: {provider.name}")
    for step in provider.steps:
        print(f"  • {step}")
    url = provider.app_password_url
    if not url:
        return
    print(f"\nApp-password page: {url}")
    try:
        answer = input_fn("Open this page in your browser now? [Y/n] ")
    except (EOFError, OSError):
        answer = "n"  # non-interactive (no stdin) — don't try to open
    if answer.strip().lower() in ("", "y", "yes"):
        try:
            open_url_fn(url)
        except Exception:  # noqa: BLE001 — a browser hiccup must not abort setup
            print(f"  (couldn't open a browser — visit {url} manually)")


def _rollback(
    account_name: str,
    email: str,
    cli_email: str | None,
    *,
    cli_host: str | None = None,
    cli_port: int | None = None,
) -> None:
    """Undo a just-written Keychain entry (+ any --email login override and
    --host/--port server override) so a rejected password never leaves a
    broken item that get_imap_password would happily return."""
    try:
        delete_imap_password(account_name, email)
    except MailKeychainError:
        pass
    if cli_email:
        delete_login_override(account_name)
    if cli_host or cli_port:
        delete_server_override(account_name)


def _prompt_write_verify(
    *,
    account_name: str,
    email: str,
    host: str,
    port: int,
    cli_email: str | None,
    cli_host: str | None,
    cli_port: int | None,
    getpass_fn: Callable[[str], str],
    imap_factory: Callable[[str, int, str, str], ImapConnector],
) -> int:
    """Prompt for the app password, write it to Keychain, and LOGIN-verify —
    retrying on a rejected password (paste-and-verify, up to
    ``_MAX_PASSWORD_ATTEMPTS``) and rolling back between attempts. A network/
    protocol error keeps the entry (can't verify now); returns the exit code.
    """
    for attempt in range(1, _MAX_PASSWORD_ATTEMPTS + 1):
        last = attempt == _MAX_PASSWORD_ATTEMPTS
        try:
            password = getpass_fn("Enter app-specific password: ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            return 1
        if not password:
            print("ERROR: empty password.", file=sys.stderr)
            if last:
                return 1
            continue

        try:
            set_imap_password(account_name, email, password)
        except MailKeychainError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(
            f"Stored in Keychain as 'apple-mail-fast-mcp.imap.{account_name}'."
        )
        # Persist an explicit --email as a login override so runtime resolution
        # uses the same login we verify here — otherwise runtime re-derives it
        # from Mail.app and ignores --email (#341).
        if cli_email:
            set_login_override(account_name, email)
        # Persist explicit --host/--port so runtime resolution connects to the
        # same server coordinates we verify here — otherwise runtime re-derives
        # them from Mail.app, which may misreport the port (#405).
        if cli_host or cli_port:
            set_server_override(account_name, host=cli_host, port=cli_port)

        print(f"Testing IMAP connection to {host}:{port}...")
        imap = imap_factory(host, port, email, password)
        try:
            # Cheap read-only call: exercises login + folder select.
            imap.search_messages(mailbox="INBOX", limit=1)
        except LoginError as exc:
            _rollback(
                account_name, email, cli_email,
                cli_host=cli_host, cli_port=cli_port,
            )
            if not last:
                print(
                    f"  Login rejected ({exc}) — the entry was removed; "
                    "try again.",
                    file=sys.stderr,
                )
                continue
            print(
                f"ERROR: IMAP login was rejected ({exc}). The Keychain entry "
                "has been removed; please re-run with the correct password.",
                file=sys.stderr,
            )
            _maybe_print_icloud_login_hint(cli_email, host, email)
            return 1
        except (OSError, IMAPClientError) as exc:
            # Network / protocol error — password may be fine but unverifiable
            # now. Keep the entry; warn explicitly. (Not a retry case.)
            print(
                f"WARNING: IMAP verification could not complete ({exc}). The "
                "Keychain entry has been written but was not verified against "
                "the server. Re-run setup-imap later or test live to confirm.",
                file=sys.stderr,
            )
            # A TLS handshake failure on a non-993 port is the #405 shape:
            # Mail.app handed us the plaintext/STARTTLS port for a server that
            # actually speaks implicit TLS on 993. Point the user at the fix.
            if port != 993:
                print(
                    f"  Hint: connecting on port {port} with implicit TLS. If "
                    "your account uses SSL on the standard IMAPS port, re-run "
                    "with `--port 993`.",
                    file=sys.stderr,
                )
            return 0

        print(f"OK (connected to {host}:{port})")
        print("Setup complete.")
        return 0
    return 1  # unreachable — the loop returns on every path


def run_setup_imap(
    *,
    account_name: str,
    cli_email: str | None,
    uninstall: bool,
    cli_host: str | None = None,
    cli_port: int | None = None,
    connector_factory: Callable[[], AppleMailConnector] | None = None,
    getpass_fn: Callable[[str], str] | None = None,
    imap_factory: Callable[
        [str, int, str, str], ImapConnector
    ] | None = None,
    open_url_fn: Callable[[str], object] | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> int:
    """Run the ``setup-imap`` subcommand. Returns the desired exit code.

    The ``*_factory`` / ``*_fn`` keyword-only arguments are injection seams for
    unit tests. Production callers omit them and get the real implementations.
    """
    mail = (connector_factory or AppleMailConnector)()
    accounts = mail.list_accounts()

    if not _account_exists(accounts, account_name):
        print(
            f"ERROR: No Mail.app account named {account_name!r}. "
            "Available accounts:",
            file=sys.stderr,
        )
        _print_available_accounts(accounts)
        return 1

    # Resolve IMAP config upfront. The connector calls _resolve_imap_config
    # at every runtime IMAP request to pick the keychain key — so we use
    # the same email here as the keychain key, otherwise setup writes one
    # entry and runtime looks for another. (#201)
    try:
        host, port, resolved_email = mail._resolve_imap_config(account_name)
    except MailAccountNotFoundError:
        print(
            f"ERROR: Mail.app stopped recognizing {account_name!r} "
            "between the account list and the IMAP config lookup.",
            file=sys.stderr,
        )
        return 1

    # `--email` is the escape hatch for the rare case where Mail.app's
    # `user name` is empty or wrong; default to the resolved value so
    # setup, uninstall, and runtime all share one keychain key.
    email = cli_email or resolved_email
    if not email:
        print(
            f"ERROR: Account {account_name!r} has no IMAP login username "
            "in Mail.app (neither `user name` nor `email addresses`). "
            "Pass --email <email> to set one explicitly.",
            file=sys.stderr,
        )
        return 1

    if uninstall:
        try:
            delete_imap_password(account_name, email)
        except MailKeychainEntryNotFoundError:
            print(
                f"No Keychain entry to remove for {account_name!r} "
                f"({email}).",
                file=sys.stderr,
            )
            return 1
        except MailKeychainError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        # Also drop any persisted login override (#341) and server override
        # (#405) so a re-setup starts from Mail.app's derived values rather
        # than stale overrides.
        delete_login_override(account_name)
        delete_server_override(account_name)
        print(f"Removed Keychain entry for {account_name!r} ({email}).")
        return 0

    # `--host` / `--port` override the Mail.app-derived server coordinates for
    # both the verification connect below and (persisted) runtime resolution —
    # the escape hatch for accounts Mail.app misreports (e.g. Zimbra returning
    # port 143 for a 993 server). (#405)
    host = cli_host or host
    port = cli_port or port

    print(f"Found Mail.app account {account_name!r} (email: {email}).")
    _offer_app_password_page(
        detect_provider(host, email),
        open_url_fn=open_url_fn or webbrowser.open,
        input_fn=input_fn or input,
    )
    return _prompt_write_verify(
        account_name=account_name,
        email=email,
        host=host,
        port=port,
        cli_email=cli_email,
        cli_host=cli_host,
        cli_port=cli_port,
        getpass_fn=getpass_fn or getpass.getpass,
        imap_factory=imap_factory or ImapConnector,
    )


def run_sync_answered(
    *,
    accounts: list[str],
    emails: list[str] | None = None,
    host: str | None = None,
    port: int | None = None,
    since_days: int = 3,
    dry_run: bool = False,
    connector_factory: Callable[[], AppleMailConnector] | None = None,
    imap_factory: Callable[[str, int, str, str], ImapConnector] | None = None,
) -> int:
    """Run the ``sync-answered`` subcommand (meant for a scheduled job).

    For each account: read the recent Sent mail, and set \\Answered (Mail's
    reply arrow) on the mails those messages replied to. When ``emails`` and
    ``host``/``port`` are given the run never talks to Mail.app (no
    AppleScript), which is what a launchd job needs; otherwise the IMAP
    coordinates are resolved through Mail.app like the MCP server does.
    Returns 0 when every account succeeded, 1 otherwise.
    """
    if emails and len(emails) != len(accounts):
        print(
            "ERROR: pass one --email per --account (same order), or none.",
            file=sys.stderr,
        )
        return 2
    factory = imap_factory or ImapConnector
    mail: AppleMailConnector | None = None
    exit_code = 0
    for index, account in enumerate(accounts):
        try:
            if emails and host and port:
                email = emails[index]
                imap_host, imap_port = host, port
            else:
                if mail is None:
                    mail = (connector_factory or AppleMailConnector)()
                imap_host, imap_port, resolved = mail._resolve_imap_config(account)
                email = emails[index] if emails else resolved
                imap_host = host or imap_host
                imap_port = port or imap_port
            password = get_imap_password(account, email)
            result = factory(imap_host, imap_port, email, password).sync_answered(
                since_days=since_days, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            print(f"{account}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        label = "would flag" if dry_run else "flagged"
        total = sum(result["flagged"].values())
        detail = ", ".join(f"{k}={v}" for k, v in result["flagged"].items()) or "-"
        print(
            f"{account}: {result['replies_found']} replies in "
            f"{'/'.join(result['sent_mailboxes']) or '?'}; {label} {total} ({detail})"
        )
        for folder, reason in result["skipped"].items():
            print(f"  skipped {folder}: {reason}", file=sys.stderr)
    return exit_code


_JOB_LABEL_PREFIX = "nl.rxgroup.mail-sync-answered"


def _job_label(account: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", account.lower()).strip("-") or "account"
    return f"{_JOB_LABEL_PREFIX}.{slug}"


def _default_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def run_install_sync_answered(
    *,
    accounts: list[str],
    interval_minutes: int = 10,
    since_days: int = 3,
    uninstall: bool = False,
    connector_factory: Callable[[], AppleMailConnector] | None = None,
    binary: str | None = None,
    agents_dir: Path | None = None,
    log_dir: Path | None = None,
    launchctl: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    uid: int | None = None,
) -> int:
    """Install (or remove) one launchd job per account that runs
    ``sync-answered`` every ``interval_minutes`` minutes.

    Run it in Terminal: the IMAP host, port and login are resolved through
    Mail.app *now* and written into the job, so the background job itself
    never needs Mail.app or AppleScript access. Returns 0 on success.
    """
    if launchctl is None and sys.platform != "darwin":
        print("ERROR: launchd jobs only exist on macOS.", file=sys.stderr)
        return 1
    if not 1 <= interval_minutes <= 1440:
        print("ERROR: --interval-minutes must be 1-1440.", file=sys.stderr)
        return 2
    run_launchctl = launchctl or _default_launchctl
    agents = agents_dir or Path.home() / "Library" / "LaunchAgents"
    logs = log_dir or Path.home() / "Library" / "Logs"
    domain = f"gui/{uid if uid is not None else os.getuid()}"

    if uninstall:
        if accounts:
            plists = [agents / f"{_job_label(a)}.plist" for a in accounts]
        else:
            plists = sorted(agents.glob(f"{_JOB_LABEL_PREFIX}.*.plist"))
        removed = 0
        for plist in plists:
            if not plist.exists():
                continue
            run_launchctl(["bootout", f"{domain}/{plist.stem}"])
            plist.unlink()
            removed += 1
            print(f"Removed {plist.stem}")
        if removed == 0:
            print("No sync-answered jobs found.")
        return 0

    if not accounts:
        print("ERROR: pass at least one --account.", file=sys.stderr)
        return 2
    exe = Path(binary or sys.argv[0]).resolve()
    if not exe.is_file() or not os.access(exe, os.X_OK):
        print(f"ERROR: cannot find the apple-mail-fast-mcp program at {exe}.",
              file=sys.stderr)
        return 1

    mail = (connector_factory or AppleMailConnector)()
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    exit_code = 0
    for account in accounts:
        try:
            host, port, email = mail._resolve_imap_config(account)
            get_imap_password(account, email)
        except MailKeychainEntryNotFoundError:
            print(
                f"{account}: no password in the Keychain yet. Run "
                f"`apple-mail-fast-mcp setup-imap --account \"{account}\" "
                f"--port {993}` first.",
                file=sys.stderr,
            )
            exit_code = 1
            continue
        except Exception as exc:  # noqa: BLE001 - report per account, continue
            print(f"{account}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        label = _job_label(account)
        plist_path = agents / f"{label}.plist"
        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "Label": label,
                    "ProgramArguments": [
                        str(exe), "sync-answered",
                        "--account", account,
                        "--email", email,
                        "--host", host,
                        "--port", str(port),
                        "--since-days", str(since_days),
                    ],
                    "StartInterval": interval_minutes * 60,
                    "RunAtLoad": True,
                    "StandardOutPath": str(logs / "mail-sync-answered.log"),
                    "StandardErrorPath": str(logs / "mail-sync-answered.log"),
                }
            )
        )
        run_launchctl(["bootout", f"{domain}/{label}"])
        result = run_launchctl(["bootstrap", domain, str(plist_path)])
        if result.returncode != 0:
            print(
                f"{account}: launchctl failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
            exit_code = 1
            continue
        print(
            f"{account}: installed, runs every {interval_minutes} minutes "
            f"(log: {logs / 'mail-sync-answered.log'})"
        )
    return exit_code


__all__ = [
    "run_setup_imap",
    "run_sync_answered",
    "run_install_sync_answered",
    "get_imap_password",
]

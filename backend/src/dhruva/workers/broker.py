"""``dhruva-broker zerodha`` -- enrol, log in, and check where you stand.

Three subcommands, and between them they are the whole of the owner's broker
authentication. ``enrol`` stores the Kite application credential; ``login``
turns a browser sign-in into a stored session; ``status`` says which of those
two is missing when something downstream reports no data.

**There is no option on this command through which a secret can be passed.**
Not ``--api-secret``, not ``--access-token``, not ``--request-token``. Every
sensitive value is typed at a hidden prompt, which means none of them reaches
PowerShell history, a shell's ``.bash_history``, a CI log, a process list, or a
screenshot of a terminal. That is a property of the argument parser rather than
a convention, and there is a test that reads the parser and asserts it.

This command places no orders and fetches no market data. It authenticates, and
authenticating is all it can do: the objects it is handed can store a credential
and perform one handshake, and there is no code path from here to an order.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import TYPE_CHECKING

import httpx2

from dhruva.contexts.platform.application.broker import (
    DescribeBrokerAuthentication,
    EnrolBrokerApplication,
    EstablishBrokerSession,
)
from dhruva.contexts.platform.domain.broker.session import BrokerApplication, BrokerAuthState
from dhruva.contexts.platform.infrastructure.crypto import MasterKeyProvider
from dhruva.contexts.platform.infrastructure.crypto.credentials import (
    open_credential,
    seal_credential,
)
from dhruva.contexts.platform.infrastructure.database.engine import (
    build_engine,
    build_session_factory,
)
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.platform.infrastructure.prompts import HiddenPrompt
from dhruva.contexts.platform.infrastructure.zerodha import KiteAuthenticator
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import DhruvaError, NotFoundError, ValidationError
from dhruva.shared.time.clock import SystemClock
from dhruva.workers.cli_arguments import parse_account

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from dhruva.contexts.platform.domain.broker.ports import SecretPrompt
    from dhruva.shared.identity import AccountId

__all__ = ["build_parser", "main"]

_BROKER = "zerodha"
_EXIT_OK = 0
_EXIT_REFUSED = 2
_EXIT_NOT_AUTHENTICATED = 3

_SAFETY = (
    "Authentication only. Stores an encrypted broker credential and completes "
    "the official browser-mediated Kite login: no order placement, no holdings "
    "or positions sync, no market-data fetch, no scheduler, and no automated "
    "password or TOTP entry. Secrets are never accepted as command-line "
    "arguments; every sensitive value is typed at a hidden prompt."
)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Every option here is non-sensitive by construction, and the corresponding
    test asserts that no option name looks like a secret. Adding one would be
    the single change that undoes the guarantee in the module docstring.
    """
    parser = argparse.ArgumentParser(
        prog="dhruva-broker",
        description=(
            "Enrol Zerodha application credentials and establish a broker "
            "session, both stored encrypted and locally."
        ),
        epilog=_SAFETY,
    )
    broker = parser.add_subparsers(dest="broker", required=True)
    zerodha = broker.add_parser("zerodha", help="Zerodha / Kite Connect")
    actions = zerodha.add_subparsers(dest="action", required=True)

    for name, help_text in (
        ("enrol", "store or rotate the Kite application credential"),
        ("login", "complete the manual browser login and store the session"),
        ("status", "report whether this account can authenticate, and why not"),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument(
            "--account",
            required=True,
            help=(
                "DHRUVA account: an identifier (acct_<uuid> or a bare UUID), or "
                "a stable lowercase label such as 'owner-family'. This is an "
                "internal label and is NOT your Zerodha client ID."
            ),
        )
    return parser


async def run(argv: Sequence[str] | None = None, *, prompt: SecretPrompt | None = None) -> int:
    """Dispatch one subcommand against a freshly built engine."""
    args = build_parser().parse_args(argv)
    account_id = parse_account(args.account)
    settings = load_settings()
    key_provider = MasterKeyProvider(settings.crypto.master_key)
    clock = SystemClock()
    ask: SecretPrompt = prompt if prompt is not None else HiddenPrompt()

    engine = build_engine(settings.db)
    session_factory: async_sessionmaker[AsyncSession] = build_session_factory(engine)

    def unit_of_work(account: AccountId) -> SqlAlchemyIdentityUnitOfWork:
        return SqlAlchemyIdentityUnitOfWork(session_factory, account_id=account)

    try:
        if args.action == "enrol":
            return await _enrol(unit_of_work, key_provider, clock, account_id, ask)
        if args.action == "login":
            return await _login(unit_of_work, key_provider, clock, account_id, ask)
        return await _status(unit_of_work, key_provider, clock, account_id)
    finally:
        await engine.dispose()


async def _enrol(
    unit_of_work: object,
    key_provider: MasterKeyProvider,
    clock: SystemClock,
    account_id: AccountId,
    ask: SecretPrompt,
) -> int:
    """Collect the application credential at hidden prompts and seal it."""
    sys.stdout.write(
        f"Enrolling Kite application credentials for {account_id} / {_BROKER}.\n"
        "Both values are read without echo and are never written to your shell "
        "history. Take them from https://developers.kite.trade -> your app.\n\n"
    )
    identifier = ask("Kite API key: ")
    secret = ask("Kite API secret: ")

    use_case = EnrolBrokerApplication(unit_of_work, key_provider, seal_credential)  # type: ignore[arg-type]
    result = await use_case.execute(
        account_id,
        _BROKER,
        BrokerApplication(identifier=identifier, secret=secret),
        at=clock.now(),
    )

    sys.stdout.write(
        ("\nRotated" if result.rotated else "\nStored")
        + f" the {_BROKER} application credential.\n"
        f"    account : {result.account_id}\n"
        f"    version : {result.version}\n"
        f"    at      : {result.stored_at.isoformat()}\n"
        "\nYour broker session was not touched. Run "
        f"'dhruva-broker zerodha login --account …' to establish one.\n"
    )
    return _EXIT_OK


async def _login(
    unit_of_work: object,
    key_provider: MasterKeyProvider,
    clock: SystemClock,
    account_id: AccountId,
    ask: SecretPrompt,
) -> int:
    """Print the official login URL, take the request token, store the session."""
    async with httpx2.AsyncClient(timeout=15.0) as client:
        use_case = EstablishBrokerSession(
            unit_of_work,  # type: ignore[arg-type]
            key_provider,
            seal_credential,
            open_credential,
            KiteAuthenticator(client, clock),
            clock,
        )
        try:
            url = await use_case.login_url(account_id, _BROKER)
        except NotFoundError:
            sys.stderr.write(
                f"No Kite application credential is enrolled for {account_id}.\n"
                "Run 'dhruva-broker zerodha enrol --account …' first.\n"
            )
            return _EXIT_NOT_AUTHENTICATED

        sys.stdout.write(
            "Open this URL in your browser and sign in to Zerodha as you "
            "normally would:\n\n"
            f"    {url}\n\n"
            "After the login completes, your browser is sent to the redirect "
            "URL registered for this app. Copy the value of the "
            "'request_token' query parameter from that address bar.\n"
            "It is valid for a few minutes and can be used once.\n\n"
        )
        request_token = ask("Kite request_token: ")
        result = await use_case.execute(account_id, _BROKER, request_token)

    sys.stdout.write(
        ("\nReplaced the previous session." if result.replaced_previous else "\nSession stored.")
        + "\n"
        f"    account        : {result.account_id}\n"
        f"    broker user id : {result.broker_user_id}\n"
        f"    issued at      : {result.issued_at.isoformat()}\n"
        f"    expires at     : {result.expires_at.isoformat()}\n"
        "\nThe access token is stored encrypted and is never displayed. Your "
        "enrolled application credential was not touched.\n"
    )
    return _EXIT_OK


async def _status(
    unit_of_work: object,
    key_provider: MasterKeyProvider,
    clock: SystemClock,
    account_id: AccountId,
) -> int:
    """Report the authentication state without changing it."""
    status = await DescribeBrokerAuthentication(
        unit_of_work,  # type: ignore[arg-type]
        key_provider,
        open_credential,
        clock,
    ).execute(account_id, _BROKER)

    lines = [
        f"account : {status.account_id}",
        f"broker  : {status.broker}",
        f"state   : {status.state.value}",
    ]
    if status.broker_user_id is not None:
        lines.append(f"broker user id : {status.broker_user_id}")
    if status.issued_at is not None:
        lines.append(f"session issued : {status.issued_at.isoformat()}")
    if status.expires_at is not None:
        lines.append(f"session expires: {status.expires_at.isoformat()}")
    if status.enrolment_rotated_at is not None:
        lines.append(f"secret rotated : {status.enrolment_rotated_at.isoformat()}")
    sys.stdout.write("\n".join(lines) + "\n")

    remedy = {
        BrokerAuthState.ENROLMENT_MISSING: (
            "\nNothing is enrolled. Run 'dhruva-broker zerodha enrol --account …'.\n"
        ),
        BrokerAuthState.SESSION_MISSING: (
            "\nEnrolled, but never logged in. Run 'dhruva-broker zerodha login --account …'.\n"
        ),
        BrokerAuthState.SESSION_EXPIRED: (
            "\nThe session has expired -- Kite sessions end at 06:00 IST the "
            "morning after they are created. Run "
            "'dhruva-broker zerodha login --account …' again.\n"
        ),
    }.get(status.state)
    if remedy is not None:
        sys.stdout.write(remedy)
        return _EXIT_NOT_AUTHENTICATED
    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point."""
    try:
        return asyncio.run(run(argv))
    except ValidationError as error:
        sys.stderr.write(f"{error}\n")
        return _EXIT_REFUSED
    except DhruvaError as error:
        sys.stderr.write(f"{error}\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    raise SystemExit(main())

"""``dhruva-reference seed`` loads committed configuration and touches no provider.

The command exists because every read command in the system worked only against
rows a test had inserted. So the tests that matter most here are the ones about
what it *is not*: no network, no credential, no broker, and an account rule that
cannot silently disagree with the commands that read what it writes.
"""

from __future__ import annotations

import ast
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from dhruva.contexts.reference.api import InstrumentKind
from dhruva.contexts.reference.infrastructure import (
    OWNER_UNIVERSE_REVISION,
    load_owner_universe,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import AccountId
from dhruva.workers import digest as digest_cli
from dhruva.workers import seed_reference as cli
from dhruva.workers.cli_arguments import parse_account

pytestmark = pytest.mark.unit

RECORDED: Final = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
ACCOUNT: Final = AccountId.deterministic("owner-family")

#: Anything that would mean the command reached a provider. The point of this
#: command is that it runs on a laptop with no broker relationship at all.
_NETWORK_MODULES = frozenset(
    {"httpx", "httpx2", "requests", "urllib", "urllib3", "socket", "http", "aiohttp"}
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited DHRUVA_* variables so tests do not affect each other."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="module")
def command_source() -> str:
    """Read the seed command's own source, for the import assertions."""
    return Path(cli.__file__).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# What it must not do
# --------------------------------------------------------------------------- #


def test_the_command_imports_no_network_library(command_source: str) -> None:
    """It reads a committed file. Nothing about that needs a socket."""
    tree = ast.parse(command_source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert not imported & _NETWORK_MODULES, f"network import: {imported & _NETWORK_MODULES}"


def test_the_command_imports_no_broker_adapter(command_source: str) -> None:
    """Instrument discovery and daily bars need a broker; this must not.

    Asserted over what the module *imports* rather than over its text. Prose
    that says "no credential" contains the word "credential", and a guard that
    could be tripped by its own denial would eventually be silenced rather than
    obeyed.
    """
    imported = _imported_names(command_source)

    for forbidden in ("zerodha", "kite", "credential", "secret", "vault", "crypto"):
        offenders = {name for name in imported if forbidden in name.lower()}
        assert not offenders, f"the seed command imports {offenders}"


def _imported_names(source: str) -> set[str]:
    """Return every module and symbol name the module imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    return names


def test_the_safety_wording_states_every_boundary() -> None:
    """Somebody reading --help is deciding whether this is safe to run."""
    epilog = cli.build_parser().epilog or ""

    for claim in ("no network", "no broker credential", "NSE", "order", "scheduler"):
        assert claim in epilog, f"the help text does not mention {claim!r}"
    assert "recommendation" in epilog


# --------------------------------------------------------------------------- #
# The command surface
# --------------------------------------------------------------------------- #


def test_a_subcommand_is_required() -> None:
    """``dhruva-reference`` alone must not silently write to a database."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_the_account_is_required() -> None:
    """A watchlist belongs to somebody, and reads are attributed."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["seed"])


def test_the_run_can_be_rehearsed() -> None:
    """Reading what would be written before writing it is the cheap safeguard."""
    args = cli.build_parser().parse_args(["seed", "--account", "owner-family", "--dry-run"])

    assert args.dry_run is True


def test_the_recorded_instant_can_be_supplied_explicitly() -> None:
    """Knowledge time is an input, so a replay can reproduce a past write."""
    args = cli.build_parser().parse_args(
        ["seed", "--account", "owner-family", "--recorded-at", "2026-08-07T12:00:00+00:00"]
    )

    assert args.recorded_at == "2026-08-07T12:00:00+00:00"


def test_universe_readiness_is_network_free_and_names_the_universe() -> None:
    """Inspection is a distinct read command and never masquerades as seed."""
    args = cli.build_parser().parse_args(
        [
            "universe-readiness",
            "--account",
            "owner-family",
            "--universe",
            "licensed-india-v1",
            "--as-of",
            "2020-01-03T10:00:00Z",
        ]
    )

    assert args.command == "universe-readiness"
    assert args.universe == "licensed-india-v1"


# --------------------------------------------------------------------------- #
# The account rule, shared with everything that reads
# --------------------------------------------------------------------------- #


def test_a_stable_label_always_resolves_to_the_same_account() -> None:
    """The point of the label form: no UUID to keep somewhere and retype."""
    assert parse_account("owner-family") == parse_account("owner-family") == ACCOUNT


def test_an_identifier_round_trips_through_its_own_text() -> None:
    """Whatever the seed prints can be pasted straight into the read commands."""
    derived = parse_account("owner-family")

    assert parse_account(str(derived)) == derived
    assert parse_account(str(derived.value)) == derived


def _account_parser_of(module: object) -> object:
    """Return the account parser a command actually calls."""
    return module.parse_account  # type: ignore[attr-defined]  # module attribute by design


def test_the_seed_and_the_digest_resolve_an_account_identically() -> None:
    """Seeding under one account and reading under another is the silent failure.

    It produces an empty digest that looks like a bug in the digest. Asserting
    the two commands hold the *same function* is stronger than asserting they
    agree on one input, which they would even if one later grew its own copy.
    """
    assert _account_parser_of(digest_cli) is _account_parser_of(cli) is parse_account


@pytest.mark.parametrize("bad", ["Owner-Family", "owner family", "x", "", "9lives", "-lead"])
def test_an_unusable_account_is_refused_rather_than_minted(bad: str) -> None:
    """Silently deriving an account from a typo creates a second empty watchlist."""
    with pytest.raises(ValidationError, match="--account"):
        parse_account(bad)


def test_the_refusal_names_both_accepted_forms() -> None:
    """Being told what is wrong without being told what is right is unhelpful."""
    with pytest.raises(ValidationError, match="acct_<uuid>"):
        parse_account("Not An Account")


# --------------------------------------------------------------------------- #
# What it loads
# --------------------------------------------------------------------------- #


def test_the_committed_configuration_carries_the_approved_watchlist() -> None:
    """Twenty approved equities plus the Nifty 50 benchmark identity."""
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    watchlisted = [item for item in command.definitions if item.included_in_watchlist]

    assert len(watchlisted) == 20
    assert all(item.kind is InstrumentKind.EQUITY for item in watchlisted)


def test_the_benchmark_is_stored_as_identity_but_not_watchlisted() -> None:
    """It is market context, not something the owner asked to follow."""
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    excluded = [item for item in command.definitions if not item.included_in_watchlist]

    assert [item.canonical_symbol for item in excluded] == ["NIFTY 50"]
    assert excluded[0].kind is InstrumentKind.INDEX


def test_the_exact_approved_symbols_are_loaded() -> None:
    """DHRUVA did not choose these; the configuration is owner input.

    Pinned exactly, punctuation included, because a symbol quietly changing
    shape would break entity linking and market-data mapping at once.
    """
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)
    symbols = {item.canonical_symbol for item in command.definitions if item.included_in_watchlist}

    assert symbols == {
        "ABCAPITAL",
        "ADANIENSOL",
        "ADANIENT",
        "ADANIGREEN",
        "ADANIPORTS",
        "ADANIPOWER",
        "BAJFINANCE",
        "CANBK",
        "COCHINSHIP",
        "ETERNAL",
        "HAL",
        "HDFCAMC",
        "ICICIBANK",
        "INDIGO",
        "M&M",
        "MAZDOCK",
        "NAM-INDIA",
        "PNB",
        "SBIN",
        "TMPV",
    }


def test_the_configuration_carries_its_own_revision() -> None:
    """Every written row records which configuration produced it."""
    command = load_owner_universe(ACCOUNT, recorded_at=RECORDED)

    assert command.source_revision == OWNER_UNIVERSE_REVISION
    assert command.source == "owner_configuration"


def test_the_recorded_instant_reaches_the_command_unchanged() -> None:
    """Knowledge time is what makes a replay reproduce a past write."""
    assert load_owner_universe(ACCOUNT, recorded_at=RECORDED).recorded_at == RECORDED


def test_loading_twice_produces_an_identical_command() -> None:
    """Idempotency starts here: the same input must describe the same write."""
    assert load_owner_universe(ACCOUNT, recorded_at=RECORDED) == load_owner_universe(
        ACCOUNT, recorded_at=RECORDED
    )

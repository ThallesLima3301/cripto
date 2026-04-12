"""Tests for `crypto_monitor.cli.main`.

The CLI is a thin shell over the lower layers, so these tests focus
on the wiring rather than re-testing business logic. The strategy:

* For ``init``, ``buy add/list``, and ``signals list`` we drive the
  real code end to end against an in-memory project root: a copy of
  ``config.example.toml`` lives in ``tmp_path`` and the SQLite file
  is created on disk under that root. These commands are pure local
  I/O — no network, no scheduling — so the round-trip is cheap.

* For ``scan``, ``weekly``, and ``evaluate`` we monkey-patch the
  scheduler entrypoints the CLI imports (`run_scan`, `run_weekly`,
  `run_maintenance`) with stand-ins. The scheduler itself is already
  exhaustively tested in ``test_scheduler.py``; here we only verify
  that the CLI dispatches to it and renders the result correctly.

* For ``ntfy-test`` we monkey-patch ``send_ntfy`` so no HTTP escapes.

* For argparse plumbing we drive ``main`` with ``stdout`` / ``stderr``
  redirected to ``StringIO`` so test assertions inspect captured text
  directly. Argparse usage errors call ``sys.exit(2)`` which we catch
  via ``pytest.raises(SystemExit)``.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import crypto_monitor.cli.main as cli_main_module  # noqa: F401  (also loads the module)
from crypto_monitor.cli.main import main
from crypto_monitor.notifications.ntfy import REASON_SENT, SendResult
from crypto_monitor.reports.weekly import WeeklyRunResult, WeeklySummary
from crypto_monitor.scheduler.entrypoints import (
    MaintenanceReport,
    ScanReport,
)


UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.toml"

# `crypto_monitor.cli` re-exports `main` from `crypto_monitor.cli.main`,
# which means the dotted name `crypto_monitor.cli.main` resolves (in the
# package's namespace) to the function rather than the submodule. We
# grab the real module out of `sys.modules` so monkeypatch can reach
# the symbols the CLI imports at module scope.
CLI_MODULE = sys.modules["crypto_monitor.cli.main"]


# ---------- fixtures ----------

@pytest.fixture
def cli_project(tmp_path: Path) -> Path:
    """A throw-away project root with the real example config copied in.

    The example file is the canonical default; using it (instead of a
    hand-written stub) means these tests fail loudly if the shipped
    config drifts in a way the CLI cannot handle.
    """
    shutil.copyfile(EXAMPLE_CONFIG, tmp_path / "config.example.toml")
    return tmp_path


def _run(*args: str) -> tuple[int, str, str]:
    """Invoke ``main`` with captured stdout/stderr.

    Returns ``(exit_code, stdout_text, stderr_text)``.
    """
    out = io.StringIO()
    err = io.StringIO()
    code = main(list(args), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# ---------- init ----------

def test_init_creates_config_initializes_db_and_seeds(cli_project: Path) -> None:
    code, out, err = _run("--project-root", str(cli_project), "init")

    assert code == 0, err
    # config.toml is materialized from the example.
    assert (cli_project / "config.toml").exists()
    # The DB was created at the path the example points to.
    db_path = cli_project / "data" / "crypto_monitor.db"
    assert db_path.exists()

    assert "created" in out
    assert "initialized database" in out
    # Example config tracks 3 symbols (BTC/ETH/SOL) with auto_seed=true.
    assert "seeded 3 tracked symbol(s)" in out


def test_init_skips_seeding_with_no_seed_flag(cli_project: Path) -> None:
    code, out, _ = _run("--project-root", str(cli_project), "init", "--no-seed")

    assert code == 0
    assert "skipped symbol seeding" in out
    assert "seeded" not in out


def test_init_does_not_overwrite_existing_config(cli_project: Path) -> None:
    """A pre-existing config.toml must be preserved verbatim."""
    custom = "# user already edited this\n"
    (cli_project / "config.toml").write_text(
        (cli_project / "config.example.toml").read_text() + custom,
        encoding="utf-8",
    )

    code, out, _ = _run("--project-root", str(cli_project), "init", "--no-seed")

    assert code == 0
    assert "config already exists" in out
    assert custom in (cli_project / "config.toml").read_text(encoding="utf-8")


def test_init_errors_when_no_config_or_example(tmp_path: Path) -> None:
    code, _, err = _run("--project-root", str(tmp_path), "init")

    assert code == 1
    assert "config.example.toml" in err


# ---------- buy add / buy list ----------

def test_buy_add_then_list_round_trip(cli_project: Path) -> None:
    # Init first so the DB and tables exist.
    init_code, _, _ = _run("--project-root", str(cli_project), "init", "--no-seed")
    assert init_code == 0

    add_code, add_out, add_err = _run(
        "--project-root", str(cli_project),
        "buy", "add",
        "--symbol", "BTCUSDT",
        "--price", "50000",
        "--amount", "100",
        "--bought-at", "2026-04-10T12:00:00Z",
        "--note", "first nibble",
    )
    assert add_code == 0, add_err
    assert "recorded buy id=1" in add_out
    assert "BTCUSDT" in add_out
    assert "2026-04-10T12:00:00Z" in add_out

    list_code, list_out, _ = _run(
        "--project-root", str(cli_project), "buy", "list"
    )
    assert list_code == 0
    assert "BTCUSDT" in list_out
    assert "first nibble" in list_out
    # Header is rendered when there's at least one row.
    assert "id" in list_out and "price" in list_out


def test_buy_add_with_explicit_quantity_overrides_derivation(
    cli_project: Path,
) -> None:
    _run("--project-root", str(cli_project), "init", "--no-seed")

    code, out, err = _run(
        "--project-root", str(cli_project),
        "buy", "add",
        "--symbol", "ETHUSDT",
        "--price", "2000",
        "--amount", "100",
        "--quantity", "0.04",  # not 100/2000=0.05
        "--bought-at", "2026-04-09T08:00:00Z",
    )
    assert code == 0, err
    # Format spec is %g so 0.04 prints exactly.
    assert "qty=0.04" in out


def test_buy_add_rejects_unparseable_timestamp(cli_project: Path) -> None:
    _run("--project-root", str(cli_project), "init", "--no-seed")

    code, _, err = _run(
        "--project-root", str(cli_project),
        "buy", "add",
        "--symbol", "BTCUSDT",
        "--price", "50000",
        "--amount", "100",
        "--bought-at", "not-a-date",
    )
    assert code == 1
    assert "could not parse" in err


def test_buy_list_empty_message(cli_project: Path) -> None:
    _run("--project-root", str(cli_project), "init", "--no-seed")

    code, out, _ = _run(
        "--project-root", str(cli_project), "buy", "list"
    )
    assert code == 0
    assert "(no buys recorded)" in out


# ---------- signals list ----------

def test_signals_list_empty_message(cli_project: Path) -> None:
    _run("--project-root", str(cli_project), "init", "--no-seed")

    code, out, _ = _run(
        "--project-root", str(cli_project), "signals", "list"
    )
    assert code == 0
    assert "(no signals recorded)" in out


def test_signals_list_renders_seeded_rows(cli_project: Path) -> None:
    """Insert two signal rows by hand and check that the formatter
    surfaces both, newest-first."""
    _run("--project-root", str(cli_project), "init", "--no-seed")

    # Open the on-disk DB the CLI just created and seed two signals.
    from crypto_monitor.database.connection import get_connection

    db_path = cli_project / "data" / "crypto_monitor.db"
    conn = get_connection(db_path)
    try:
        conn.execute(
            """
            INSERT INTO signals (
                symbol, detected_at, candle_hour, price_at_signal,
                score, severity, trigger_reason, score_breakdown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                "BTCUSDT", "2026-04-10T10:00:00Z", "2026-04-10T10:00:00Z",
                40000.0, 70, "strong", "drop_24h>=5",
            ),
        )
        conn.execute(
            """
            INSERT INTO signals (
                symbol, detected_at, candle_hour, price_at_signal,
                score, severity, trigger_reason, score_breakdown
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')
            """,
            (
                "ETHUSDT", "2026-04-11T09:00:00Z", "2026-04-11T09:00:00Z",
                2000.0, 85, "very_strong", "drop_7d>=15",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    code, out, _ = _run(
        "--project-root", str(cli_project), "signals", "list"
    )
    assert code == 0
    assert "BTCUSDT" in out
    assert "ETHUSDT" in out
    assert "very strong" in out  # underscores stripped for display
    # Newest-first ordering: ETH (Apr 11) appears before BTC (Apr 10).
    assert out.index("ETHUSDT") < out.index("BTCUSDT")


def test_signals_list_filters_by_severity(cli_project: Path) -> None:
    _run("--project-root", str(cli_project), "init", "--no-seed")

    from crypto_monitor.database.connection import get_connection

    db_path = cli_project / "data" / "crypto_monitor.db"
    conn = get_connection(db_path)
    try:
        for sev, score, sym in (
            ("normal", 55, "BTCUSDT"),
            ("very_strong", 90, "ETHUSDT"),
        ):
            conn.execute(
                """
                INSERT INTO signals (
                    symbol, detected_at, candle_hour, price_at_signal,
                    score, severity, trigger_reason, score_breakdown
                ) VALUES (?, ?, ?, ?, ?, ?, 'test', '{}')
                """,
                (
                    sym, "2026-04-10T10:00:00Z", "2026-04-10T10:00:00Z",
                    100.0, score, sev,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    code, out, _ = _run(
        "--project-root", str(cli_project),
        "signals", "list", "--severity", "very_strong",
    )
    assert code == 0
    assert "ETHUSDT" in out
    assert "BTCUSDT" not in out


# ---------- scan / weekly / evaluate (monkeypatched scheduler) ----------

@dataclass
class _RunCall:
    project_root: Path
    extras: dict[str, Any]


def test_cmd_scan_dispatches_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`scan` should call run_scan with the project root and print its summary."""
    captured: list[_RunCall] = []

    def fake_run_scan(*, project_root: Path, **kwargs: Any) -> ScanReport:
        captured.append(_RunCall(project_root=project_root, extras=kwargs))
        return ScanReport()

    monkeypatch.setattr(CLI_MODULE, "run_scan", fake_run_scan)

    code, out, _ = _run("--project-root", str(tmp_path), "scan")

    assert code == 0
    assert len(captured) == 1
    assert captured[0].project_root == tmp_path.resolve()
    assert "scan" in out  # summary_line() starts with "scan ..."


def test_cmd_scan_returns_nonzero_when_report_has_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run_scan(**_: Any) -> ScanReport:
        return ScanReport(errors=["ingest: boom"])

    monkeypatch.setattr(CLI_MODULE, "run_scan", fake_run_scan)

    code, _, _ = _run("--project-root", str(tmp_path), "scan")
    assert code == 1


def test_cmd_weekly_prints_summary_and_handles_send_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = WeeklySummary(
        week_start="2026-04-04T00:00:00Z",
        week_end="2026-04-11T00:00:00Z",
        signal_count=4,
        signal_by_severity={"strong": 2, "normal": 2},
        buy_count=1,
        top_drop_symbol="BTCUSDT",
        top_drop_pct=-7.5,
        matured_count=0,
        verdict_counts={},
        body="weekly body",
    )
    sent_result = SendResult(sent=False, reason="missing_topic")

    def fake_run_weekly(**_: Any) -> WeeklyRunResult:
        return WeeklyRunResult(
            summary=summary, summary_id=42, send_result=sent_result
        )

    monkeypatch.setattr(CLI_MODULE, "run_weekly", fake_run_weekly)

    code, out, _ = _run("--project-root", str(tmp_path), "weekly")
    assert code == 1  # send failed
    assert "id=42" in out
    assert "signals=4" in out
    assert "buys=1" in out
    assert "sent=no" in out


def test_cmd_weekly_success_returns_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = WeeklySummary(
        week_start="2026-04-04T00:00:00Z",
        week_end="2026-04-11T00:00:00Z",
        signal_count=0,
        signal_by_severity={},
        buy_count=0,
        top_drop_symbol=None,
        top_drop_pct=None,
        matured_count=0,
        verdict_counts={},
        body="quiet week",
    )
    ok = SendResult(sent=True, reason=REASON_SENT, status_code=200)

    monkeypatch.setattr(
        CLI_MODULE,
        "run_weekly",
        lambda **_: WeeklyRunResult(
            summary=summary, summary_id=7, send_result=ok
        ),
    )

    code, out, _ = _run("--project-root", str(tmp_path), "weekly")
    assert code == 0
    assert "sent=yes" in out


def test_cmd_evaluate_dispatches_and_prints_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: list[_RunCall] = []

    def fake_run_maintenance(*, project_root: Path, **kwargs: Any) -> MaintenanceReport:
        captured.append(_RunCall(project_root=project_root, extras=kwargs))
        return MaintenanceReport()

    monkeypatch.setattr(CLI_MODULE, "run_maintenance", fake_run_maintenance)

    code, out, _ = _run("--project-root", str(tmp_path), "evaluate")

    assert code == 0
    assert len(captured) == 1
    assert captured[0].project_root == tmp_path.resolve()
    assert "maintenance" in out


def test_cmd_evaluate_nonzero_on_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        CLI_MODULE,
        "run_maintenance",
        lambda **_: MaintenanceReport(errors=["prune: boom"]),
    )

    code, _, _ = _run("--project-root", str(tmp_path), "evaluate")
    assert code == 1


# ---------- ntfy-test ----------

def test_cmd_ntfy_test_success(
    monkeypatch: pytest.MonkeyPatch, cli_project: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_send_ntfy(
        ntfy: Any, title: str, body: str, **kwargs: Any
    ) -> SendResult:
        captured["title"] = title
        captured["body"] = body
        captured["kwargs"] = kwargs
        return SendResult(sent=True, reason=REASON_SENT, status_code=200)

    monkeypatch.setattr(CLI_MODULE, "send_ntfy", fake_send_ntfy)

    code, out, _ = _run(
        "--project-root", str(cli_project),
        "ntfy-test",
        "--title", "hello",
        "--body", "world",
    )
    assert code == 0
    assert "sent ok status=200" in out
    assert captured["title"] == "hello"
    assert captured["body"] == "world"
    assert captured["kwargs"]["priority"] == "default"
    assert captured["kwargs"]["tags"] == ("test",)


def test_cmd_ntfy_test_failure(
    monkeypatch: pytest.MonkeyPatch, cli_project: Path
) -> None:
    monkeypatch.setattr(
        CLI_MODULE,
        "send_ntfy",
        lambda *a, **kw: SendResult(
            sent=False, reason="missing_topic", error="no topic"
        ),
    )

    code, _, err = _run(
        "--project-root", str(cli_project), "ntfy-test"
    )
    assert code == 1
    assert "ntfy test failed" in err
    assert "missing_topic" in err


# ---------- argparse / dispatcher edge cases ----------

def test_main_returns_systemexit_on_unknown_command(tmp_path: Path) -> None:
    """argparse exits with code 2 when the subcommand is missing/unknown."""
    with pytest.raises(SystemExit) as info:
        _run("--project-root", str(tmp_path), "nonsense")
    assert info.value.code == 2


def test_main_requires_a_subcommand(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        _run("--project-root", str(tmp_path))
    assert info.value.code == 2


# ---------- module entrypoint (`python -m crypto_monitor.cli`) ----------

def test_module_entrypoint_runs_real_init(tmp_path: Path) -> None:
    """`python -m crypto_monitor.cli` must launch the CLI in the real
    package layout — not just via in-process calls to `main()`.

    This is the only test in the suite that shells out to a child
    Python. It guards a regression none of the in-process tests can
    catch: a missing or broken ``crypto_monitor/cli/__main__.py`` would
    leave every in-process test green while ``python -m`` fails for
    real users. We drive ``init --no-seed`` because it's the cheapest
    command that exercises the full parser → handler → settings → DB
    pipeline and leaves an observable artifact (config + db) on disk.
    """
    shutil.copyfile(EXAMPLE_CONFIG, tmp_path / "config.example.toml")

    # Make sure the child process can find the source tree even if the
    # package isn't installed in site-packages. We prepend the repo
    # root to PYTHONPATH the same way pytest's rootdir resolution does.
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    )

    result = subprocess.run(
        [
            sys.executable, "-m", "crypto_monitor.cli",
            "--project-root", str(tmp_path),
            "init", "--no-seed",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=30,
    )

    assert result.returncode == 0, (
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "initialized database" in result.stdout
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "data" / "crypto_monitor.db").exists()

"""
Command-line entry points of the one-time, checkout-abandonment and
receivables agents.

Found by running the README commands with Ollama down (2026-11-02): the
checkout and receivables agents retried every record against the dead host
(3 attempts with backoff each, past a 2-minute timeout for 30 records), and
the one-time agent exited 0 after writing a RESULTS file in which all 30
records had fallen back to policy. `python -m failsafe.agent` and
`failsafe.integrated_pipeline` already failed fast with a health check; these now
do too. A bad record count used to be a ValueError traceback, and a negative
one silently dropped records from the end (`carts[:-5]`).

`run()` is replaced by a recorder in every test, so nothing here processes
records, writes outputs or contacts Ollama/Razorpay.
"""

import pytest

from failsafe import agent_onetime, checkout_abandonment_agent, ollama_client, receivables_agent

COUNTED = [checkout_abandonment_agent, receivables_agent]
ALL = [agent_onetime, *COUNTED]


@pytest.fixture
def run_calls(monkeypatch):
    calls: list[tuple] = []

    async def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    for mod in ALL:
        monkeypatch.setattr(mod, "run", fake_run)
    return calls


@pytest.fixture
def ollama_down(monkeypatch):
    monkeypatch.setattr(
        ollama_client,
        "healthcheck",
        lambda: (False, "Ollama not reachable at http://127.0.0.1:9: refused"),
    )


@pytest.fixture
def ollama_up(monkeypatch):
    monkeypatch.setattr(ollama_client, "healthcheck", lambda: (True, "Ollama reachable (test)"))


@pytest.mark.parametrize("mod", ALL, ids=lambda m: m.__name__)
def test_fails_fast_before_any_record_when_ollama_is_down(mod, run_calls, ollama_down):
    with pytest.raises(SystemExit) as exc:
        mod.main([])
    msg = str(exc.value.code)
    assert msg.startswith("Healthcheck failed: Ollama not reachable")
    assert "--skip-healthcheck" in msg
    assert run_calls == []


@pytest.mark.parametrize("mod", ALL, ids=lambda m: m.__name__)
def test_skip_healthcheck_bypasses_the_check(mod, run_calls, monkeypatch):
    def must_not_be_called():
        raise AssertionError("healthcheck called despite --skip-healthcheck")

    monkeypatch.setattr(ollama_client, "healthcheck", must_not_be_called)
    mod.main(["--skip-healthcheck"])
    assert len(run_calls) == 1


@pytest.mark.parametrize("mod", ALL, ids=lambda m: m.__name__)
def test_healthy_run_reports_and_proceeds(mod, run_calls, ollama_up, capsys):
    mod.main([])
    assert len(run_calls) == 1
    assert "[healthcheck] Ollama reachable (test)" in capsys.readouterr().out


@pytest.mark.parametrize("mod", COUNTED, ids=lambda m: m.__name__)
@pytest.mark.parametrize(("argv", "expected_n"), [([], None), (["30"], 30), (["0"], 0)])
def test_record_count_is_passed_through(mod, argv, expected_n, run_calls, ollama_up):
    mod.main(argv)
    assert run_calls == [((expected_n,), {})]


@pytest.mark.parametrize("mod", COUNTED, ids=lambda m: m.__name__)
@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ("abc", "expected a whole number of records, got 'abc'"),
        ("2.5", "expected a whole number of records, got '2.5'"),
        ("-5", "record count must be 0 or more, got -5"),
    ],
)
def test_bad_record_count_is_a_usage_error_not_a_traceback(
    mod, bad, message, run_calls, ollama_up, capsys
):
    with pytest.raises(SystemExit) as exc:
        mod.main([bad])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert run_calls == []


def test_onetime_rejects_a_stray_record_count(run_calls, ollama_up, capsys):
    # agent_onetime always processes the whole file; a count used to be ignored silently
    with pytest.raises(SystemExit) as exc:
        agent_onetime.main(["30"])
    assert exc.value.code == 2
    assert "unrecognized arguments: 30" in capsys.readouterr().err
    assert run_calls == []


def test_require_ollama_skip_never_calls_healthcheck(monkeypatch):
    monkeypatch.setattr(ollama_client, "healthcheck", lambda: pytest.fail("called"))
    ollama_client.require_ollama(skip=True)


# ------------------------------------------------- integrated pipeline CLI


@pytest.fixture
def integrated_calls(monkeypatch):
    from failsafe import integrated_pipeline

    calls: list[tuple] = []

    async def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(integrated_pipeline, "run", fake_run)
    return integrated_pipeline, calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], ((15,), {"skip_healthcheck": False})),
        (["4", "--skip-healthcheck"], ((4,), {"skip_healthcheck": True})),
    ],
)
def test_integrated_main_passes_arguments_through(integrated_calls, argv, expected):
    integrated_pipeline, calls = integrated_calls
    integrated_pipeline.main(argv)
    assert calls == [expected]


def test_integrated_main_rejects_negative_per_domain(integrated_calls, capsys):
    integrated_pipeline, calls = integrated_calls
    with pytest.raises(SystemExit) as exc:
        integrated_pipeline.main(["-1"])
    assert exc.value.code == 2
    assert "record count must be 0 or more, got -1" in capsys.readouterr().err
    assert calls == []

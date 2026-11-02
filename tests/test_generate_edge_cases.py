import pytest

from failsafe.generate_checkout_abandonment_data import generate as gen_checkout
from failsafe.generate_data import generate as gen_main
from failsafe.generate_data_onetime import generate as gen_onetime
from failsafe.generate_detection_pool import generate as gen_detect
from failsafe.generate_receivables_data import generate as gen_recv


@pytest.mark.parametrize("gen", [gen_main, gen_recv, gen_checkout, gen_detect, gen_onetime])
def test_generate_n_zero_returns_empty(gen):
    assert gen(n=0, seed=42) == []


@pytest.mark.parametrize("gen", [gen_main, gen_recv, gen_checkout, gen_detect, gen_onetime])
def test_generate_n_one_returns_single(gen):
    records = gen(n=1, seed=42)
    assert len(records) == 1


@pytest.mark.parametrize("gen", [gen_main, gen_recv, gen_checkout, gen_detect, gen_onetime])
def test_generate_deterministic(gen):
    a = gen(n=10, seed=123)
    b = gen(n=10, seed=123)
    for ra, rb in zip(a, b):
        for k in set(ra.keys()) & set(rb.keys()):
            if k in (
                "subscription_id",
                "customer_id",
                "invoice_id",
                "payment_id",
                "cart_id",
                "cart_token",
                "abandonment_id",
            ):
                continue
            assert ra[k] == rb[k]


def test_generate_huge_n():
    records = gen_main(n=1000, seed=42)
    assert len(records) == 1000
    assert len({r["subscription_id"] for r in records}) == 1000


def test_generate_large_n_unique_ids_receivables():
    records = gen_recv(n=500, seed=42)
    assert len(records) == 500
    assert len({r["invoice_id"] for r in records}) == 500


def test_generate_checkout_huge_n():
    records = gen_checkout(n=500, seed=1)
    assert len(records) == 500

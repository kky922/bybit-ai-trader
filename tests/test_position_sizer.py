from trading.position_sizer import compute_size


def test_compute_size_limits():
    size = compute_size(
        equity_usdt=1000,
        entry=10,
        stop=9,
        lot_step=0.1,
        min_amount=0.1,
        min_notional=5,
    )
    assert size > 0
    # Floating point Safety: check that size rounds to 0.1 step
    assert abs(round(size, 1) - size) < 1e-10


def test_compute_size_min_notional_block():
    size = compute_size(
        equity_usdt=50,
        entry=1000,
        stop=900,
        lot_step=0.001,
        min_amount=0.001,
        min_notional=5,
    )
    assert size == 0.0

import inspect

from stock_watch_list_back_end import calculate_chip_distribution


def test_chip_distribution_default_window_is_60d():
    signature = inspect.signature(calculate_chip_distribution)

    assert signature.parameters["days"].default == "60d"

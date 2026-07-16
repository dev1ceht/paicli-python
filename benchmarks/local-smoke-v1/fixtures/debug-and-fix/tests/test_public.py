from src.handler import process


def test_process_sums_three_values():
    assert process([1, 2, 3]) == 6

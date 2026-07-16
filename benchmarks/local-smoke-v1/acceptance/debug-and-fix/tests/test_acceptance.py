from src.handler import process


def test_process_handles_input_boundaries():
    assert process([]) == 0
    assert process([7]) == 7
    assert process([-2, 5, -1, 9]) == 11

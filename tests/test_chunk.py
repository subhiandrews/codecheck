from codecheck.core.chunk import chunk_files


def test_splits_by_count_and_chars():
    sizes = {"a": 10, "b": 10, "c": 10, "d": 10}
    assert chunk_files(sizes, max_files=2, max_chars=1000) == [["a", "b"], ["c", "d"]]
    assert chunk_files(sizes, max_files=10, max_chars=25) == [["a", "b"], ["c", "d"]]


def test_oversized_file_gets_its_own_chunk_not_skipped():
    assert chunk_files({"a": 5, "huge": 999, "b": 5}, max_files=10, max_chars=100) == [["a"], ["huge"], ["b"]]


def test_empty():
    assert chunk_files({}, max_files=5, max_chars=5) == []

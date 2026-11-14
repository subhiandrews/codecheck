import codecheck


def test_version_is_exposed():
    assert codecheck.__version__ == "0.1.3"

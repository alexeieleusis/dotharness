from spec_prism_flow.sizing import check_file_scope, check_word_count


def test_check_file_scope_in_band():
    result = check_file_scope([f"f{i}.py" for i in range(7)])

    assert result.count == 7
    assert result.in_band is True
    assert result.over_ceiling is False
    assert result.note is None


def test_check_file_scope_under_band_flagged_not_failed():
    result = check_file_scope(["a.py", "b.py"])

    assert result.count == 2
    assert result.in_band is False
    assert result.over_ceiling is False
    assert result.note is not None


def test_check_file_scope_flagged_range():
    result = check_file_scope([f"f{i}.py" for i in range(13)])

    assert result.count == 13
    assert result.in_band is False
    assert result.over_ceiling is False
    assert result.note is not None


def test_check_file_scope_over_ceiling():
    result = check_file_scope([f"f{i}.py" for i in range(16)])

    assert result.count == 16
    assert result.in_band is False
    assert result.over_ceiling is True
    assert result.note is not None


def test_check_word_count_in_band():
    result = check_word_count(" ".join(["word"] * 700))

    assert result.count == 700
    assert result.in_band is True
    assert result.note is None


def test_check_word_count_in_band_but_below_observed_min():
    result = check_word_count(" ".join(["word"] * 510))

    assert result.count == 510
    assert result.in_band is True
    assert result.note is None


def test_check_word_count_out_of_band_but_observed():
    result = check_word_count(" ".join(["word"] * 1800))

    assert result.count == 1800
    assert result.in_band is False
    assert result.note is not None


def test_check_word_count_outside_observed_range():
    result = check_word_count(" ".join(["word"] * 100))

    assert result.count == 100
    assert result.in_band is False
    assert result.note is not None


def test_check_word_count_never_raises_on_empty_text():
    result = check_word_count("")

    assert result.count == 0
    assert result.in_band is False
    assert result.note is not None

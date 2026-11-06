from unittest.mock import patch

import pytest

from dotharness.pr_review.config import SubDir
from dotharness.pr_review.runners.common import get_vibe_heal_context


def test_empty_subdirs_returns_empty():
    assert get_vibe_heal_context([], "/wdir", "main") == ""


def test_missing_sonar_properties_returns_empty(tmp_path):
    subdir = SubDir(path="backend")
    (tmp_path / "backend").mkdir()
    assert get_vibe_heal_context([subdir], str(tmp_path), "main") == ""


def test_sonar_properties_with_no_matching_key_returns_empty(tmp_path):
    d = tmp_path / "sub"
    d.mkdir()
    (d / "sonar-project.properties").write_text("sonar.organization=myorg\n", encoding="utf-8")
    assert get_vibe_heal_context([SubDir(path="sub")], str(tmp_path), "main") == ""


def test_missing_review_file_returns_empty(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=tmp_path / "home"):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "main")
    assert result == ""


def test_returns_review_content_with_details_stripped(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "fe" / "my-branch"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text(
        "# Review\n\n## `src/foo.ts`\nsome finding\n<details>\nshould be removed\n</details>\n",
        encoding="utf-8",
    )
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "my-branch")
    assert "some finding" in result
    assert "<details>" not in result
    assert "should be removed" not in result


def test_branch_with_slashes_preserved_as_path(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "fe" / "user" / "feat-123"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text("branch content", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "user/feat-123")
    assert "branch content" in result


def test_deduplicates_by_project_key(tmp_path):
    for name in ("sub1", "sub2"):
        d = tmp_path / name
        d.mkdir()
        (d / "sonar-project.properties").write_text("sonar.projectKey=shared\n", encoding="utf-8")
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "shared" / "main"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text("shared content", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path="sub1"), SubDir(path="sub2")], str(tmp_path), "main")
    assert result.count("shared content") == 1


def test_concatenates_multiple_project_keys(tmp_path):
    for key, name in (("fe", "frontend"), ("be", "backend")):
        d = tmp_path / name
        d.mkdir()
        (d / "sonar-project.properties").write_text(f"sonar.projectKey={key}\n", encoding="utf-8")
    home = tmp_path / "home"
    for key, content in (("fe", "frontend findings"), ("be", "backend findings")):
        review_dir = home / ".vibe-heal" / "reviews" / key / "main"
        review_dir.mkdir(parents=True)
        (review_dir / "review.md").write_text(content, encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path="frontend"), SubDir(path="backend")], str(tmp_path), "main")
    assert "frontend findings" in result
    assert "backend findings" in result


def test_skips_subdir_without_properties_continues_to_next(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "sonar-project.properties").write_text("sonar.projectKey=good\n", encoding="utf-8")
    (tmp_path / "bad").mkdir()  # no sonar-project.properties
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "good" / "main"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text("good content", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path="bad"), SubDir(path="good")], str(tmp_path), "main")
    assert "good content" in result


def test_leading_spaces_and_comments_in_properties_ignored(tmp_path):
    (tmp_path / "sonar-project.properties").write_text(
        "# sonar.projectKey=commented-out\n  sonar.projectKey=fe  \n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "fe" / "main"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text("content", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "main")
    assert "content" in result


def test_all_details_content_returns_empty(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "fe" / "main"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text("<details>\neverything\n</details>", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "main")
    assert result == ""


def test_nested_details_tags_stripped_completely(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    home = tmp_path / "home"
    review_dir = home / ".vibe-heal" / "reviews" / "fe" / "main"
    review_dir.mkdir(parents=True)
    (review_dir / "review.md").write_text(
        "<details>\nouter <details>\ninner\n</details>\n</details>",
        encoding="utf-8",
    )
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "main")
    assert result == ""
    assert "<details>" not in result
    assert "</details>" not in result


@pytest.mark.parametrize(
    ("key", "planted_rel"),
    [
        ("../../../../tmp/evil", None),
        ("fe..\\..\\tmp\\evil", None),
        ("fe/evil", "fe/evil"),
        ("a..b", "a..b"),
        ("", None),
    ],
)
def test_unsafe_project_key_is_rejected(tmp_path, key, planted_rel):
    (tmp_path / "sonar-project.properties").write_text(f"sonar.projectKey={key}\n", encoding="utf-8")
    home = tmp_path / "home"
    reviews = home / ".vibe-heal" / "reviews"
    reviews.mkdir(parents=True)
    # Plant a review where the unsanitised key would resolve, so only rejection yields "".
    planted_dirs = [reviews / "fe" / "evil" / "main", reviews / "a..b" / "main", reviews / "main"]
    if planted_rel:
        planted_dirs.append(reviews / planted_rel / "main")
    for d in planted_dirs:
        d.mkdir(parents=True, exist_ok=True)
        (d / "review.md").write_text("PLANTED", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "main")
    assert "PLANTED" not in result
    assert result == ""


def _setup_fe_project(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    home = tmp_path / "home"
    reviews = home / ".vibe-heal" / "reviews"
    (reviews / "fe").mkdir(parents=True)
    return home, reviews


def test_branch_with_path_traversal_segments_returns_empty(tmp_path):
    home, _ = _setup_fe_project(tmp_path)
    # reviews/fe/../../../secret/review.md resolves to home/secret/review.md (a real file)
    secret_dir = home / "secret"
    secret_dir.mkdir()
    (secret_dir / "review.md").write_text("classified", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "../../../secret")
    assert result == ""


def test_branch_escaping_into_sibling_project_returns_empty(tmp_path):
    home, reviews = _setup_fe_project(tmp_path)
    # reviews/fe/../other-project/main/review.md stays inside reviews/ but outside reviews/fe
    other = reviews / "other-project" / "main"
    other.mkdir(parents=True)
    (other / "review.md").write_text("classified", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "../other-project/main")
    assert result == ""


def test_symlinked_branch_dir_pointing_outside_returns_empty(tmp_path):
    home, reviews = _setup_fe_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "review.md").write_text("classified", encoding="utf-8")
    (reviews / "fe" / "linked").symlink_to(outside, target_is_directory=True)
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "linked")
    assert result == ""

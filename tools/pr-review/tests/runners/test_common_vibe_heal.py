from unittest.mock import patch

from harness.config import SubDir
from harness.runners.common import get_vibe_heal_context


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


def test_branch_with_path_traversal_segments_returns_empty(tmp_path):
    (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=fe\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    secret_dir = home / "secret"
    secret_dir.mkdir()
    (secret_dir / "secret.txt").write_text("classified", encoding="utf-8")
    review_base = home / ".vibe-heal" / "reviews" / "fe"
    review_base.mkdir(parents=True)
    with patch("pathlib.Path.home", return_value=home):
        result = get_vibe_heal_context([SubDir(path=".")], str(tmp_path), "../../../secret/secret.txt")
    assert result == ""
    assert "classified" not in result

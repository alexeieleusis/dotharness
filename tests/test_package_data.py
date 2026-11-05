"""Knowledge and skills ship as package data and stay reachable from the repo root."""

import hashlib
from importlib import resources
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "dotharness"

# SHA-256 of each tracked file under the old root knowledge/ at commit cb0efc1 (pre-move).
KNOWLEDGE_SHA256 = {
    "patterns/react/COMPONENT_ORGANIZATION_CONVENTION.md": "2c6a3ffcb255055323370074f66511b0c828426cf2ff819a63fd820c9256497e",
    "patterns/react/QUICK_REF_PATTERNS.md": "a862de1be5bf6a93d75a9b08f8f077ad9d00a197185d361877ca1dc5f3376fb4",
    "patterns/react/QUICK_START_MIGRATION.md": "b5e52bbf85e255d767438dfcb041e7ea6aa9c938957409f7fd6aa1c459ee6ce5",
    "patterns/react/QUICK_START_NEW_COMPONENT.md": "af38bab722bcaa63561db2a27a31e81314c979f00e27dfbff7c8315cbe337868",
    "patterns/react/README.md": "9e0f93c945880084b8c7b729d16cadb988e93087d6d35ca4ca96511fef6a31e1",
    "pr-review/address-comment.md": "5cf92379f415251eeb06a6167c3907a5baf270f86bc52e7e90514a2d3c912b5f",
    "pr-review/address-comments.md": "250c23e0d005604382ef0d732baa7dd919d1428ae4967575c28f217952d5886d",
    "pr-review/ci-fix-prompt.md": "aee41d42cb556d914a0962c7cb94c4ff6b8917f98d3bc11cefb50997ee725047",
    "pr-review/focused-review.md": "3c108e1a952ec5128de9dbfbd62cfecd301c7ad95542a97e69a48f988c06ebaf",
    "pr-review/local-address-finding.md": "c9dbddac30bb03c14d5e65e1dff9ed4cc46b04cec0bd268457dc853833d1801f",
    "pr-review/local-review-design.md": "18dcf449909dae03048035f7af5f86d6137e18255869c3672b283522c0c728d4",
    "pr-review/local-review-file.md": "efd49e46fcd61b118515fbae18a8842c2cb41a1dff1cf2e82905333beffe33fa",
    "pr-review/local-review-summary.md": "14cb77f51e6bd8a05a10cd52d3a0f6094b94bb5035199b8dd9b6124a6c0f1162",
    "pr-review/review-design.md": "67edcb7caef80e10270cd9f0794d60e952101a37449d36717383cb026d85051e",
    "pr-review/review-file.md": "506f5daac9f2c302798a659477a6d84d7e3cef76ddfb002e28596712ccac9ada",
    "pr-review/review-prs.md": "1bfa0785e2d22f077fbd253cf664f081bfa9233c6ebbf4c0fc88024c1c31e9c5",
    "pr-review/review-regret.md": "594442dcfe762b50eddad7b2e56f0e2e2dc970bd04874766c5db680cdea4f88e",
    "pr-review/review-summary.md": "5a9a2e432d570d4d31c5cedbf8011fac3d1fc66a29a988399f690b16e2d7bf1b",
    "pr-review/review-traceability.md": "0f3fee691506e277494f2e82fc62eb8a3dae6fa378520d4834b100ae963a20e7",
    "sisyphus/intake-prompt.md": "7fb1d907a18bd20f76ccadeea07cd74a588dff330c85225ec331a7229a974046",
    "sisyphus/linear-feed-prompt.md": "e76652507f9fdb9aaafc933dadb1c9c76eef4597791203dcade167e70fb3eddc",
    "sisyphus/orchestration-prompt.md": "88a4f099b9d494ab527421aa797772bd032a2b28ac1768e4f64b1ed98c894cb4",
    "sisyphus/sizing-rules.md": "cceb3146e013221e4165c961136290051ff260f5bc3af63659b3ad169ddb8f2e",
    "spec-prism-flow/build-execution-prompt.md": "d93c162cc9c7781ccdb2e3f8f201231db7049e217611405123cfe1425b78a4f9",
    "spec-prism-flow/plan-generation-prompt.md": "5756c4a751264b096cd84e2879a2310619729f4f34534bf1345bda6a4606d833",
    "spec-prism-flow/requirements-doc-drafting-prompt.md": "f79153d1e5cd9ed1d21c7944a03c79fd6074b722d34201f6a7cb95cba3d9fc18",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_has_26_files() -> None:
    assert len(KNOWLEDGE_SHA256) == 26


@pytest.mark.parametrize(("rel", "digest"), sorted(KNOWLEDGE_SHA256.items()))
def test_knowledge_file_is_byte_identical(rel: str, digest: str) -> None:
    assert _sha256(PACKAGE_DIR / "knowledge" / rel) == digest


def test_knowledge_has_no_extra_files() -> None:
    root = PACKAGE_DIR / "knowledge"
    found = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert found == set(KNOWLEDGE_SHA256)


def test_root_knowledge_is_symlink_to_package() -> None:
    link = REPO_ROOT / "knowledge"
    assert link.is_symlink()
    assert link.resolve() == (PACKAGE_DIR / "knowledge").resolve()


def test_root_skills_symlink_resolves_to_skill() -> None:
    link = REPO_ROOT / "skills" / "asd-ste100"
    assert link.is_symlink()
    assert (link / "SKILL.md").resolve() == (PACKAGE_DIR / "skills" / "asd-ste100" / "SKILL.md").resolve()
    assert (link / "SKILL.md").is_file()


def test_orchestration_prompt_resolves_through_symlink() -> None:
    prompt = REPO_ROOT / "knowledge" / "sisyphus" / "orchestration-prompt.md"
    assert prompt.is_file()
    assert prompt.read_bytes() == (PACKAGE_DIR / "knowledge" / "sisyphus" / "orchestration-prompt.md").read_bytes()


def test_importlib_resources_resolves_knowledge_and_skills() -> None:
    pkg = resources.files("dotharness")
    assert (pkg / "knowledge").is_dir()
    assert (pkg / "skills").is_dir()
    assert (pkg / "knowledge" / "sisyphus" / "orchestration-prompt.md").is_file()
    assert (pkg / "skills" / "asd-ste100" / "SKILL.md").is_file()


def test_pr_comments_script_is_package_data_and_matches_runner_path():
    from dotharness.pr_review.runners.common import PR_COMMENTS_SCRIPT_PATH

    packaged = PACKAGE_DIR / "scripts" / "pr-comments.py"
    assert packaged.is_file()
    assert PR_COMMENTS_SCRIPT_PATH.resolve() == packaged.resolve()

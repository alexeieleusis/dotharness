"""Knowledge and skills ship as package data and stay reachable from the repo root."""

import hashlib
from importlib import resources
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "dotharness"

# SHA-256 of each tracked file under the old root knowledge/ at commit cb0efc1 (pre-move), except files edited since:
# the I-5 prompt rewording (spec_prism_flow naming), the lifecycle cheat sheet added for sisyphus, and the
# generic/fastapi pattern sets added after the port (4817481) and later reworded by the plain-English editing
# pass (b829cdc).
KNOWLEDGE_SHA256 = {
    "patterns/fastapi/FASTAPI_ORGANIZATION_CONVENTION.md": "76a341eb86aa32d1021840be6069f51fcc47ddf9455b2c32d8f87ff89093974e",
    "patterns/fastapi/QUICK_REF_PATTERNS.md": "b5f672fcf3ecff686c14bb3429766aa2cb0457301ebb2a946cacad5307ca3eed",
    "patterns/fastapi/QUICK_START_MIGRATION.md": "a423488170aa9fe932125f5228531d197dbbfce3d34cfecc35e47ebf0b813ebc",
    "patterns/fastapi/QUICK_START_NEW_ENDPOINT.md": "8aa6ad26bfd3dffc59404a45334f4fa203b9d7c5a8fe29e64aa719f5c2d0c3e1",
    "patterns/fastapi/README.md": "08eabfc94377311ef9c9e58575cc1abd54eb326f7dcf73789f9268a758ff24fc",
    "patterns/generic/ADAPTING_TO_A_CODEBASE.md": "147de9090d2a20645eeaffe541549988b8436494671f5159a1e5824f140d783f",
    "patterns/generic/CODE_ORGANIZATION_CONVENTION.md": "0ca47ab13a33a829d8ea65ee8a374c2092bb9bb110c37483a0483e1f7ccac29f",
    "patterns/generic/QUICK_REF_PATTERNS.md": "4f0d379abb51f1354589e023f1f51c50a2256dc06946183767fdc0eb28543e92",
    "patterns/generic/QUICK_START_MIGRATION.md": "1f2a648e645a0176695c6faf775f6250a80734ee91477d67ea18248e8af679eb",
    "patterns/generic/QUICK_START_NEW_FEATURE.md": "f9cc17a4e9d551fdb8c1037ec9940cb83ea8e1ebef694e42d06e23b663d6ac59",
    "patterns/generic/README.md": "1144e3ef40c50a4a814e3d2cffa12ec0ee35fa3fef5ba1ba86e7d5d274c039c7",
    "patterns/react/COMPONENT_ORGANIZATION_CONVENTION.md": "2c6a3ffcb255055323370074f66511b0c828426cf2ff819a63fd820c9256497e",
    "patterns/react/QUICK_REF_PATTERNS.md": "a862de1be5bf6a93d75a9b08f8f077ad9d00a197185d361877ca1dc5f3376fb4",
    "patterns/react/QUICK_START_MIGRATION.md": "b5e52bbf85e255d767438dfcb041e7ea6aa9c938957409f7fd6aa1c459ee6ce5",
    "patterns/react/QUICK_START_NEW_COMPONENT.md": "af38bab722bcaa63561db2a27a31e81314c979f00e27dfbff7c8315cbe337868",
    "patterns/react/README.md": "9e0f93c945880084b8c7b729d16cadb988e93087d6d35ca4ca96511fef6a31e1",
    "pr-review/address-comment.md": "c13ecaf2611e2eef2a3980d04bc1cfb6c196ba5d0c8e8cf7adfc6b78a5e027b0",
    "pr-review/address-comments.md": "250c23e0d005604382ef0d732baa7dd919d1428ae4967575c28f217952d5886d",
    "pr-review/ci-fix-prompt.md": "aee41d42cb556d914a0962c7cb94c4ff6b8917f98d3bc11cefb50997ee725047",
    "pr-review/focused-review.md": "4e9fe8f1eb55b8aa4304434cdbe7909ff824d2ad55931dbabcf5579f02238985",
    "pr-review/local-address-finding.md": "5bc748e734c7fdf8e3b6d52286c87b5b82dc5615f28ca5a7b6faf33aacda3838",
    "pr-review/local-review-design.md": "ea4e46e550e2ea4aef8c93704bcaa97ec06ffbc127064b9d85f02fe0eeedb764",
    "pr-review/local-review-file.md": "218b969e6ad698081c687c8c4117138b1c06689bc19ac850b1e548beeb023361",
    "pr-review/local-review-summary.md": "92bfeef0fbf88337d992858120ddd181be8c29503c59f36006c1602eccf78a2f",
    "pr-review/review-design.md": "9ea962b8fae2e3ba35419d059477a1ed167c87af3f3abe5e9cdd918a7c0be615",
    "pr-review/review-file.md": "edd2d865145b1ce68c7a34640d5c84bd9d4b98537c6494ac1fd9e9f95aebd3c7",
    "pr-review/review-prs.md": "1bfa0785e2d22f077fbd253cf664f081bfa9233c6ebbf4c0fc88024c1c31e9c5",
    "pr-review/review-regret.md": "3537507b7e614d1b301782447ecc7cc3da4d010d2c99f4ed73ae290348ef52c7",
    "pr-review/review-summary.md": "751c2cb9f080688b0b722cde80ce4eaeedfc59b3f7d0f5118074547207293640",
    "pr-review/review-traceability.md": "6d4fea050b3bf5343f1aadddf134be5b492ddff06d40a0951a13e23c925c61b3",
    "sisyphus/intake-prompt.md": "77f69497f895ac2137743f5533daa1444e30e70cea20d77d58089d7650159d87",
    "sisyphus/linear-feed-prompt.md": "e76652507f9fdb9aaafc933dadb1c9c76eef4597791203dcade167e70fb3eddc",
    "sisyphus/orchestration-prompt.md": "00a740a2e90866a446cb602ecdb3d0d2190082c6eca50c3794b250382b808364",
    "sisyphus/sizing-rules.md": "953ed3e744d39f8fbe7196303fd3dcad4eae70f98d49adbccb5530532260cc36",
    "spec-prism-flow/build-execution-prompt.md": "8764c4017d60180c928e4418e1de376baf1c8d58ef1fc47351c56eba0a86673c",
    "spec-prism-flow/lifecycle-cheat-sheet.md": "259c2a79eb31f41b1740c438f045538f3b19efe4950afbb9bd0ef2d17c2c2b9f",
    "spec-prism-flow/plan-generation-prompt.md": "4651068890fe6b2bd948e3ab6e3b7eed442aec6a1d8f33dfd0b6e74489982041",
    "spec-prism-flow/requirements-doc-drafting-prompt.md": "72524c62db898e102e06b33645bee7a129d2ab7ce5a018e5a1b49fa86f31a95c",
}


# Files added after the byte-identical port; covered by their own tests, so no digest is pinned.
NEW_KNOWLEDGE_FILES = {"sisyphus/refactor-prompt.md"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_has_38_files() -> None:
    assert len(KNOWLEDGE_SHA256) == 38


@pytest.mark.parametrize(("rel", "digest"), sorted(KNOWLEDGE_SHA256.items()))
def test_knowledge_file_is_byte_identical(rel: str, digest: str) -> None:
    assert _sha256(PACKAGE_DIR / "knowledge" / rel) == digest


def test_knowledge_has_no_extra_files() -> None:
    root = PACKAGE_DIR / "knowledge"
    found = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert found == set(KNOWLEDGE_SHA256) | NEW_KNOWLEDGE_FILES


def test_root_skills_symlink_resolves_to_skill() -> None:
    link = REPO_ROOT / "skills" / "asd-ste100"
    assert link.is_symlink()
    assert (link / "SKILL.md").resolve() == (PACKAGE_DIR / "skills" / "asd-ste100" / "SKILL.md").resolve()
    assert (link / "SKILL.md").is_file()


def test_importlib_resources_resolves_knowledge_and_skills() -> None:
    pkg = resources.files("dotharness")
    assert (pkg / "knowledge").is_dir()
    assert (pkg / "skills").is_dir()
    assert (pkg / "knowledge" / "sisyphus" / "orchestration-prompt.md").is_file()
    assert (pkg / "skills" / "asd-ste100" / "SKILL.md").is_file()


def test_pr_comments_script_is_package_data_and_matches_runner_path():
    from dotharness.pr_review.runners.common import PR_COMMENTS_SCRIPT_PATH

    packaged = PACKAGE_DIR / "code_host" / "scripts" / "pr-comments.py"
    assert packaged.is_file()
    assert PR_COMMENTS_SCRIPT_PATH.resolve() == packaged.resolve()


def test_pr_comments_script_path_is_provided_by_the_reference_host():
    from dotharness.code_host.reference import PR_COMMENTS_SCRIPT
    from dotharness.pr_review.runners.common import PR_COMMENTS_SCRIPT_PATH

    assert PR_COMMENTS_SCRIPT_PATH == PR_COMMENTS_SCRIPT
    assert not (PACKAGE_DIR.parent.parent / "docs" / "pr_review" / "scripts" / "pr-comments.py").exists()


async def test_reference_host_provides_the_comments_script():
    from dotharness.code_host import HostSelection, create_code_host
    from dotharness.code_host.reference import PR_COMMENTS_SCRIPT

    host = create_code_host(HostSelection(setting="host", name="reference", options={}, token_cmd="echo t"))  # noqa: S106
    assert await host.discussion.comments_script() == PR_COMMENTS_SCRIPT
    assert PR_COMMENTS_SCRIPT.parent == PACKAGE_DIR / "code_host" / "scripts"
    assert PR_COMMENTS_SCRIPT.is_file()

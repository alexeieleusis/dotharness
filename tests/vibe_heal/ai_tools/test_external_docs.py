"""Tests for external_docs URL extraction and fetching."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from dotharness.vibe_heal.ai_tools.external_docs import (
    _MAX_DOC_BYTES,
    _is_safe_url,
    _vibe_types_local_path,
    extract_urls,
    fetch_external_rule_docs,
    fetch_url_content,
    fetch_vibe_types_knowledge_docs,
    is_vibe_types_doc_url,
)


@pytest.fixture(autouse=True)
def _fake_public_dns():
    """Resolve non-literal hostnames to a public address so tests don't need network DNS."""
    import socket

    real = socket.getaddrinfo

    def fake(host, *args, **kwargs):
        return real(host, *args, **kwargs) if _is_literal(host) else [(2, 1, 6, "", ("93.184.216.34", 0))]

    def _is_literal(host: str) -> bool:
        import ipaddress

        try:
            ipaddress.ip_address(host)
        except ValueError:
            return host.isdigit() or host.startswith("0x") or host in {"localhost"}
        return True

    with patch("socket.getaddrinfo", fake):
        yield


class TestIsSafeUrl:
    def test_localhost_blocked(self) -> None:
        assert not _is_safe_url("http://localhost/evil")

    def test_loopback_ip_blocked(self) -> None:
        assert not _is_safe_url("http://127.0.0.1/evil")

    def test_ipv6_loopback_blocked(self) -> None:
        assert not _is_safe_url("http://[::1]/evil")

    def test_private_class_a_blocked(self) -> None:
        assert not _is_safe_url("http://10.0.0.1/evil")

    def test_private_class_b_blocked(self) -> None:
        assert not _is_safe_url("http://172.16.0.1/evil")

    def test_private_class_c_blocked(self) -> None:
        assert not _is_safe_url("http://192.168.1.1/evil")

    def test_other_loopback_blocked(self) -> None:
        assert not _is_safe_url("http://127.0.0.2/")

    def test_ipv4_mapped_loopback_blocked(self) -> None:
        assert not _is_safe_url("http://[::ffff:127.0.0.1]/")

    def test_ipv4_mapped_private_blocked(self) -> None:
        assert not _is_safe_url("http://[::ffff:10.0.0.1]/")

    def test_unspecified_and_cgnat_blocked(self) -> None:
        assert not _is_safe_url("http://0.0.0.0/")
        assert not _is_safe_url("http://100.64.0.1/")

    def test_decimal_ip_blocked(self) -> None:
        assert not _is_safe_url("http://2130706433/")

    def test_dns_name_resolving_to_private_blocked(self) -> None:
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            assert not _is_safe_url("http://127.0.0.1.nip.io/")

    def test_unresolvable_host_blocked(self) -> None:
        with patch("socket.getaddrinfo", side_effect=OSError):
            assert not _is_safe_url("http://nonexistent.invalid/")

    def test_public_hostname_allowed(self) -> None:
        assert _is_safe_url("https://next.sonarqube.com/sonarqube/coding_rules")

    def test_public_ip_allowed(self) -> None:
        assert _is_safe_url("https://1.1.1.1/doc")


class TestExtractUrls:
    def test_single_url(self) -> None:
        urls = extract_urls("See https://example.com/rule for details.")
        assert urls == ["https://example.com/rule"]

    def test_multiple_urls(self) -> None:
        urls = extract_urls("See https://example.com/a and https://example.com/b.")
        assert urls == ["https://example.com/a", "https://example.com/b"]

    def test_no_urls(self) -> None:
        assert extract_urls("No links here at all.") == []

    def test_http_and_https(self) -> None:
        urls = extract_urls("http://old.example.com and https://new.example.com")
        assert len(urls) == 2

    def test_empty_string(self) -> None:
        assert extract_urls("") == []


class TestVibeTypesLocalPath:
    def test_raw_refs_heads_url(self) -> None:
        url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        result = _vibe_types_local_path(url)
        assert result is not None
        assert result.as_posix().endswith(
            "vendor/vibe-types/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        )

    def test_raw_main_url(self) -> None:
        url = "https://raw.githubusercontent.com/jpablo/vibe-types/main/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        result = _vibe_types_local_path(url)
        assert result is not None
        assert str(result).endswith("plugin/skills/typescript/catalog/T01-algebraic-data-types.md")

    def test_blob_url(self) -> None:
        url = "https://github.com/jpablo/vibe-types/blob/main/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        result = _vibe_types_local_path(url)
        assert result is not None
        assert str(result).endswith("plugin/skills/typescript/catalog/T01-algebraic-data-types.md")

    def test_non_vibe_types_url_returns_none(self) -> None:
        assert _vibe_types_local_path("https://example.com/doc.md") is None

    def test_other_github_repo_returns_none(self) -> None:
        assert _vibe_types_local_path("https://github.com/someother/repo/blob/main/file.md") is None

    def test_path_traversal_returns_none(self) -> None:
        url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/../../etc/passwd"
        assert _vibe_types_local_path(url) is None

    def test_url_with_query_string_strips_query(self) -> None:
        url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/file.md?token=secret"
        result = _vibe_types_local_path(url)
        assert result is not None
        assert result.as_posix().endswith("vendor/vibe-types/plugin/file.md")


class TestFetchUrlContent:
    @pytest.mark.asyncio
    @respx.mock
    async def test_successful_fetch(self) -> None:
        respx.get("https://example.com/doc.md").mock(return_value=httpx.Response(200, text="# Rule\nDo not do X."))
        content = await fetch_url_content("https://example.com/doc.md")
        assert content == "# Rule\nDo not do X."

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_returns_none(self) -> None:
        respx.get("https://example.com/missing.md").mock(return_value=httpx.Response(404))
        content = await fetch_url_content("https://example.com/missing.md")
        assert content is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_network_error_returns_none(self) -> None:
        respx.get("https://example.com/error.md").mock(side_effect=httpx.ConnectError("refused"))
        content = await fetch_url_content("https://example.com/error.md")
        assert content is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_to_internal_address_blocked(self) -> None:
        respx.get("https://attacker.example/r").mock(
            return_value=httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        )
        internal = respx.get("http://169.254.169.254/latest/meta-data/").mock(
            return_value=httpx.Response(200, text="secret")
        )
        content = await fetch_url_content("https://attacker.example/r")
        assert content is None
        assert not internal.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_safe_redirect_followed(self) -> None:
        respx.get("https://example.com/a").mock(return_value=httpx.Response(302, headers={"Location": "/b"}))
        respx.get("https://example.com/b").mock(return_value=httpx.Response(200, text="ok"))
        assert await fetch_url_content("https://example.com/a") == "ok"

    @pytest.mark.asyncio
    @respx.mock
    async def test_redirect_loop_returns_none(self) -> None:
        respx.get("https://example.com/loop").mock(return_value=httpx.Response(302, headers={"Location": "/loop"}))
        assert await fetch_url_content("https://example.com/loop") is None

    @pytest.mark.asyncio
    async def test_ssrf_localhost_returns_none(self) -> None:
        content = await fetch_url_content("http://localhost/secret")
        assert content is None

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_returns_none(self) -> None:
        content = await fetch_url_content("http://192.168.1.1/secret")
        assert content is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_large_response_is_truncated(self) -> None:
        large_content = "x" * (_MAX_DOC_BYTES + 1000)
        respx.get("https://example.com/large.md").mock(return_value=httpx.Response(200, text=large_content))
        content = await fetch_url_content("https://example.com/large.md")
        assert content is not None
        assert len(content.encode("utf-8")) <= _MAX_DOC_BYTES

    @pytest.mark.asyncio
    async def test_vibe_types_url_reads_locally_when_file_exists(self, tmp_path: Path) -> None:
        local_file = tmp_path / "T01.md"
        local_file.write_text("# T01 local content")

        url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=local_file):
            content = await fetch_url_content(url)

        assert content == "# T01 local content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_local_read_oserror_falls_back_to_http(self) -> None:
        raw_url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/file.md"
        respx.get(raw_url).mock(return_value=httpx.Response(200, text="# http content"))

        mock_path = MagicMock(spec=Path)
        mock_path.exists.return_value = True
        mock_path.read_bytes.side_effect = OSError("permission denied")

        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=mock_path):
            content = await fetch_url_content(raw_url)

        assert content == "# http content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_blob_url_converts_to_raw_when_local_path_is_none(self) -> None:
        raw_url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/file.md"
        respx.get(raw_url).mock(return_value=httpx.Response(200, text="# raw content"))

        blob_url = "https://github.com/jpablo/vibe-types/blob/main/plugin/file.md"
        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=None):
            content = await fetch_url_content(blob_url)

        assert content == "# raw content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_blob_url_falls_back_to_raw_when_no_submodule(self) -> None:
        raw_url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        respx.get(raw_url).mock(return_value=httpx.Response(200, text="# fallback content"))

        blob_url = "https://github.com/jpablo/vibe-types/blob/main/plugin/skills/typescript/catalog/T01-algebraic-data-types.md"
        with patch(
            "dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path",
            return_value=Path("/nonexistent/path/T01.md"),
        ):
            content = await fetch_url_content(blob_url)

        assert content == "# fallback content"

    @pytest.mark.asyncio
    async def test_local_read_respects_max_doc_bytes(self, tmp_path: Path) -> None:
        large_file = tmp_path / "large.md"
        large_file.write_bytes(b"x" * (_MAX_DOC_BYTES + 1000))

        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=large_file):
            content = await fetch_url_content(
                "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/large.md"
            )

        assert content is not None
        assert len(content.encode("utf-8")) <= _MAX_DOC_BYTES

    @pytest.mark.asyncio
    @respx.mock
    async def test_sha_pinned_url_fetches_directly_without_fallback(self) -> None:
        sha_url = "https://raw.githubusercontent.com/jpablo/vibe-types/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2/T01.md"
        respx.get(sha_url).mock(return_value=httpx.Response(200, text="# T01 pinned content"))
        content = await fetch_url_content(sha_url)
        assert content == "# T01 pinned content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_sha_pinned_url_falls_back_to_main_on_404(self) -> None:
        sha_url = "https://raw.githubusercontent.com/jpablo/vibe-types/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2/T01.md"
        main_url = "https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md"
        respx.get(sha_url).mock(return_value=httpx.Response(404))
        respx.get(main_url).mock(return_value=httpx.Response(200, text="# T01 main content"))
        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=None):
            content = await fetch_url_content(sha_url)
        assert content == "# T01 main content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_sha_pinned_url_falls_back_to_main_on_network_error(self) -> None:
        sha_url = "https://raw.githubusercontent.com/jpablo/vibe-types/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2/T01.md"
        main_url = "https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md"
        respx.get(sha_url).mock(side_effect=httpx.ConnectError("refused"))
        respx.get(main_url).mock(return_value=httpx.Response(200, text="# T01 main content"))
        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=None):
            content = await fetch_url_content(sha_url)
        assert content == "# T01 main content"

    @pytest.mark.asyncio
    @respx.mock
    async def test_sha_pinned_and_main_both_fail_returns_none(self) -> None:
        sha_url = "https://raw.githubusercontent.com/jpablo/vibe-types/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2/T01.md"
        main_url = "https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md"
        respx.get(sha_url).mock(return_value=httpx.Response(404))
        respx.get(main_url).mock(return_value=httpx.Response(404))
        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=None):
            content = await fetch_url_content(sha_url)
        assert content is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_sha_pinned_url_falls_back_to_local_submodule_when_main_fetch_not_needed(
        self, tmp_path: Path
    ) -> None:
        sha_url = "https://raw.githubusercontent.com/jpablo/vibe-types/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2/T01.md"
        respx.get(sha_url).mock(return_value=httpx.Response(404))
        local_file = tmp_path / "T01.md"
        local_file.write_text("# T01 local content")

        def fake_local_path(url: str) -> Path | None:
            if url == "https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md":
                return local_file
            return None

        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", side_effect=fake_local_path):
            content = await fetch_url_content(sha_url)

        assert content == "# T01 local content"


class TestFetchExternalRuleDocs:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetches_url_in_message(self) -> None:
        respx.get("https://example.com/rule.md").mock(return_value=httpx.Response(200, text="# Rule doc"))
        docs = await fetch_external_rule_docs("Fix this. See https://example.com/rule.md")
        assert docs == ["# Rule doc"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_skips_failed_url(self) -> None:
        respx.get("https://example.com/ok.md").mock(return_value=httpx.Response(200, text="good"))
        respx.get("https://example.com/bad.md").mock(return_value=httpx.Response(500))
        docs = await fetch_external_rule_docs("See https://example.com/ok.md and https://example.com/bad.md")
        assert docs == ["good"]

    @pytest.mark.asyncio
    async def test_no_urls_returns_empty(self) -> None:
        docs = await fetch_external_rule_docs("No links in this message.")
        assert docs == []

    @pytest.mark.asyncio
    async def test_ssrf_url_in_message_is_skipped(self) -> None:
        docs = await fetch_external_rule_docs("See http://192.168.1.1/rule for details.")
        assert docs == []

    @pytest.mark.asyncio
    async def test_vibe_types_url_in_message_reads_locally(self, tmp_path: Path) -> None:
        local_file = tmp_path / "T01.md"
        local_file.write_text("# local doc content")

        url = "https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/plugin/T01.md"
        with patch("dotharness.vibe_heal.ai_tools.external_docs._vibe_types_local_path", return_value=local_file):
            docs = await fetch_external_rule_docs(f"See {url} for details.")

        assert docs == ["# local doc content"]


class TestIsVibeTypesDocUrl:
    def test_main_raw_form(self) -> None:
        assert is_vibe_types_doc_url("https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md")

    def test_refs_heads_main_raw_form(self) -> None:
        assert is_vibe_types_doc_url("https://raw.githubusercontent.com/jpablo/vibe-types/refs/heads/main/T01.md")

    def test_blob_form(self) -> None:
        assert is_vibe_types_doc_url("https://github.com/jpablo/vibe-types/blob/main/T01.md")

    def test_sha_pinned_form(self) -> None:
        assert is_vibe_types_doc_url(
            "https://raw.githubusercontent.com/jpablo/vibe-types/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2/T01.md"
        )

    def test_unrelated_url_is_false(self) -> None:
        assert not is_vibe_types_doc_url("https://example.com/rule.md")


class TestFetchVibeTypesKnowledgeDocs:
    @pytest.mark.asyncio
    @respx.mock
    async def test_fetches_only_vibe_types_url_from_mixed_message(self) -> None:
        respx.get("https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md").mock(
            return_value=httpx.Response(200, text="# T01 knowledge")
        )
        message = (
            "See https://example.com/unrelated.md and "
            "https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md for guidance."
        )
        docs = await fetch_vibe_types_knowledge_docs(message)
        assert docs == ["# T01 knowledge"]

    @pytest.mark.asyncio
    async def test_no_vibe_types_url_returns_empty_list(self) -> None:
        docs = await fetch_vibe_types_knowledge_docs("Fix this. See https://example.com/rule.md")
        assert docs == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_skips_url_that_fails_to_fetch(self) -> None:
        respx.get("https://raw.githubusercontent.com/jpablo/vibe-types/main/missing.md").mock(
            return_value=httpx.Response(404)
        )
        docs = await fetch_vibe_types_knowledge_docs(
            "See https://raw.githubusercontent.com/jpablo/vibe-types/main/missing.md"
        )
        assert docs == []

    @pytest.mark.asyncio
    @respx.mock
    async def test_duplicate_url_is_fetched_once(self) -> None:
        route = respx.get("https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md").mock(
            return_value=httpx.Response(200, text="# T01 knowledge")
        )
        message = (
            "See https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md and again "
            "https://raw.githubusercontent.com/jpablo/vibe-types/main/T01.md for guidance."
        )
        docs = await fetch_vibe_types_knowledge_docs(message)
        assert docs == ["# T01 knowledge"]
        assert route.call_count == 1

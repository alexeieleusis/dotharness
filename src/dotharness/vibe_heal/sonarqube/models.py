"""Models for SonarQube API responses."""

import html
import re
from typing import Any

from pydantic import BaseModel, Field, model_validator
from typing_extensions import Self


class SonarQubeIssue(BaseModel):
    """Represents a SonarQube issue.

    Supports both old and new SonarQube API formats.
    """

    model_config = {"extra": "ignore"}  # Ignore extra fields from API

    key: str = Field(description="Unique issue identifier")
    rule: str = Field(description="Rule identifier (e.g., 'python:S1481')")
    message: str = Field(description="Issue description")
    component: str = Field(description="Component/file path")
    line: int | None = Field(default=None, description="Line number where issue occurs")

    # New API format fields
    issue_status: str | None = Field(
        default=None,
        alias="issueStatus",
        description="Issue status (new API)",
    )
    impacts: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Impact array with severity (new API)",
    )

    # Old API format fields (for backward compatibility)
    severity: str | None = Field(
        default=None,
        description="Issue severity (old API or extracted from impacts)",
    )
    status: str | None = Field(
        default=None,
        description="Issue status (old API)",
    )
    type: str | None = Field(
        default=None,
        description="Issue type (old API)",
    )

    @model_validator(mode="after")
    def extract_fields_from_new_api(self) -> Self:
        """Extract severity and status from new API format if needed."""
        # Extract severity from impacts if not already set
        if not self.severity and self.impacts:
            # Get highest severity from impacts
            self.severity = self.impacts[0].get("severity", "INFO")

        # Use issueStatus if status not set
        if not self.status and self.issue_status:
            self.status = self.issue_status

        # Default values if still not set
        if not self.severity:
            self.severity = "INFO"
        if not self.status:
            self.status = "OPEN"

        return self

    @property
    def is_fixable(self) -> bool:
        """Check if issue is potentially fixable.

        Returns:
            True if issue is fixable
        """
        # Issues without line numbers are harder to fix
        if self.line is None:
            return False
        # Don't auto-fix resolved/accepted issues
        status_upper = (self.status or "").upper()
        return status_upper not in [
            "RESOLVED",
            "CLOSED",
            "WONTFIX",
            "FALSE-POSITIVE",
            "ACCEPTED",
        ]


class DescriptionSection(BaseModel):
    """Represents a section of rule description."""

    model_config = {"extra": "ignore"}

    key: str = Field(description="Section key (e.g., 'root_cause', 'how_to_fix')")
    content: str = Field(description="HTML content of the section")


class SonarQubeRule(BaseModel):
    """Represents detailed information about a SonarQube rule.

    Retrieved from /api/rules/show endpoint.
    """

    model_config = {"extra": "ignore"}  # Ignore extra fields from API

    key: str = Field(description="Rule key (e.g., 'typescript:S3801')")
    repo: str = Field(description="Repository/language (e.g., 'typescript')")
    name: str = Field(description="Rule name")
    lang: str = Field(description="Programming language code (e.g., 'ts')")
    lang_name: str = Field(
        alias="langName",
        description="Language display name (e.g., 'TypeScript')",
    )
    severity: str = Field(description="Default severity (MAJOR, MINOR, etc.)")
    type: str = Field(description="Rule type (BUG, VULNERABILITY, CODE_SMELL)")
    description_sections: list[DescriptionSection] = Field(
        default_factory=list,
        alias="descriptionSections",
        description="Sections of the rule description",
    )

    # Optional fields
    html_desc: str | None = Field(
        default=None,
        alias="htmlDesc",
        description="HTML description (older API format)",
    )
    md_desc: str | None = Field(
        default=None,
        alias="mdDesc",
        description="Markdown description (if available)",
    )
    sys_tags: list[str] = Field(
        default_factory=list,
        alias="sysTags",
        description="System tags",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="User tags",
    )

    @property
    def markdown_description(self) -> str:
        """Get rule description in markdown format.

        Converts HTML description to markdown if needed.

        Returns:
            Rule description in markdown
        """
        # Use mdDesc if available
        if self.md_desc:
            return self.md_desc

        # Try to get description from descriptionSections
        if self.description_sections:
            # Combine all sections
            sections_text = []
            for section in self.description_sections:
                # Convert section content from HTML to markdown
                section_md = self._html_to_markdown(section.content)
                sections_text.append(section_md)
            return "\n\n".join(sections_text)

        # Fallback to htmlDesc
        if self.html_desc:
            return self._html_to_markdown(self.html_desc)

        return "No description available"

    @staticmethod
    def _html_to_markdown(html_content: str) -> str:
        """Convert HTML to basic markdown.

        Args:
            html_content: HTML string

        Returns:
            Markdown-formatted string
        """
        # Decode HTML entities
        text = html.unescape(html_content)

        # Replace common HTML tags with markdown equivalents
        text = re.sub(r"<h1>(.*?)</h1>", r"# \1\n", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<h2>(.*?)</h2>", r"## \1\n", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<h3>(.*?)</h3>", r"### \1\n", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<strong>(.*?)</strong>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<b>(.*?)</b>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<em>(.*?)</em>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<i>(.*?)</i>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<pre>(.*?)</pre>", r"```\n\1\n```\n", text, flags=re.IGNORECASE | re.DOTALL)

        # Handle lists
        text = re.sub(r"<li>(.*?)</li>", r"- \1\n", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<ul>(.*?)</ul>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"<ol>(.*?)</ol>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)

        # Remove paragraph tags but keep content
        text = re.sub(r"<p>(.*?)</p>", r"\1\n\n", text, flags=re.IGNORECASE | re.DOTALL)

        # Remove remaining HTML tags
        text = re.sub(r"<[^>]+>", "", text)

        # Clean up multiple newlines
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()

    @property
    def root_cause_html(self) -> str | None:
        """Get the HTML content of the root_cause description section."""
        for section in self.description_sections:
            if section.key == "root_cause":
                return section.content
        return None

    def as_details_block(self, summary_label: str | None = None) -> str | None:
        """Format root_cause as a GitHub collapsed <details> section.

        Args:
            summary_label: Text for the <summary> tag. Defaults to "Why: {rule name}".

        Returns:
            Formatted <details> block, or None if no root_cause section exists.
        """
        html = self.root_cause_html
        if not html:
            return None
        label = summary_label or f"Why: {self.name}"
        return f"<details>\n\n<summary>{label}</summary>\n\n{html}\n\n</details>"

    @property
    def public_doc_url(self) -> str:
        """Generate URL for public SonarSource rule documentation.

        Returns:
            URL to public rule documentation
        """
        # Use the format you discovered: https://next.sonarqube.com/sonarqube/coding_rules?open={key}&rule_key={key}
        return f"https://next.sonarqube.com/sonarqube/coding_rules?open={self.key}&rule_key={self.key}"


class RuleResponse(BaseModel):
    """Response from /api/rules/show endpoint."""

    model_config = {"extra": "ignore"}

    rule: SonarQubeRule = Field(description="Rule details")


class SourceLine(BaseModel):
    """Represents a single line of source code from SonarQube.

    Retrieved from /api/sources/lines endpoint.
    """

    model_config = {"extra": "ignore"}

    line: int = Field(description="Line number")
    code: str = Field(description="Source code (may contain HTML tags)")
    scm_revision: str | None = Field(
        default=None,
        alias="scmRevision",
        description="SCM revision hash",
    )
    scm_author: str | None = Field(
        default=None,
        alias="scmAuthor",
        description="Author email",
    )
    scm_date: str | None = Field(
        default=None,
        alias="scmDate",
        description="Date of last modification",
    )
    duplicated: bool = Field(default=False, description="Whether line is duplicated")
    is_new: bool = Field(
        default=False,
        alias="isNew",
        description="Whether line is new code",
    )
    line_hits: int | None = Field(
        default=None,
        alias="lineHits",
        description="Times this line was executed by tests. None = not instrumented.",
    )

    @property
    def plain_code(self) -> str:
        """Get source code with HTML tags removed.

        Returns:
            Plain text source code
        """
        # Remove HTML tags and decode entities
        text = re.sub(r"<[^>]+>", "", self.code)
        return html.unescape(text)


class SourceLinesResponse(BaseModel):
    """Response from /api/sources/lines endpoint."""

    model_config = {"extra": "ignore"}

    sources: list[SourceLine] = Field(default_factory=list, description="Source code lines")


class IssuesResponse(BaseModel):
    """Response from SonarQube issues API.

    Supports both old and new SonarQube API formats.
    """

    model_config = {"extra": "ignore"}  # Ignore extra fields from API

    # These can come from either top-level (old API) or paging object (new API)
    total: int | None = Field(default=None, description="Total number of issues")
    p: int | None = Field(default=None, description="Current page")
    ps: int | None = Field(default=None, description="Page size")

    issues: list[SonarQubeIssue] = Field(default_factory=list)
    paging: dict[str, Any] = Field(default_factory=dict, description="Pagination info")

    @model_validator(mode="after")
    def extract_paging_info(self) -> Self:
        """Extract pagination from paging object if not in top-level fields."""
        # Extract from paging object if top-level fields not set
        if self.total is None and "total" in self.paging:
            self.total = self.paging["total"]

        if self.p is None:
            # Try pageIndex (new API) or p (old API)
            self.p = self.paging.get("pageIndex") or self.paging.get("p", 1)

        if self.ps is None:
            # Try pageSize (new API) or ps (old API)
            self.ps = self.paging.get("pageSize") or self.paging.get("ps", 100)

        # Set defaults if still not set
        if self.total is None:
            self.total = len(self.issues)
        if self.p is None:
            self.p = 1
        if self.ps is None:
            self.ps = 100

        return self

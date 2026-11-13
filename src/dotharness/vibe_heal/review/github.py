"""Legacy error aliases; the review client now lives in `dotharness.code_host`."""

from dotharness.code_host.errors import CodeHostError, CodeHostTimeoutError, NoChangeRequestError

GitHubReviewError = CodeHostError
NoOpenPrError = NoChangeRequestError
GitHubTimeoutError = CodeHostTimeoutError

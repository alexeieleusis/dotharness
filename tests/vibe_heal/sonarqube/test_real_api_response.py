"""Test with real SonarQube API response."""

from dotharness.vibe_heal.sonarqube.models import IssuesResponse


def test_parse_real_sonarqube_response() -> None:
    """Test parsing actual SonarQube API response."""
    real_response = {
        "paging": {"pageIndex": 1, "pageSize": 100, "total": 1},
        "issues": [
            {
                "key": "01fc972e-2a3c-433e-bcae-0bd7f88f5123",
                "component": "com.github.kevinsawicki:http-request:com.github.kevinsawicki.http.HttpRequest",
                "project": "com.github.kevinsawicki:http-request",
                "rule": "java:S1144",
                "cleanCodeAttribute": "CLEAR",
                "cleanCodeAttributeCategory": "INTENTIONAL",
                "issueStatus": "ACCEPTED",
                "prioritizedRule": False,
                "impacts": [{"softwareQuality": "SECURITY", "severity": "HIGH"}],
                "message": 'Remove this unused private "getKee" method.',
                "messageFormattings": [{"start": 0, "end": 4, "type": "CODE"}],
                "line": 81,
                "hash": "a227e508d6646b55a086ee11d63b21e9",
                "author": "Developer 1",
                "effort": "2h1min",
                "creationDate": "2013-05-13T17:55:39+0200",
                "updateDate": "2013-05-13T17:55:39+0200",
                "tags": ["bug"],
                "comments": [
                    {
                        "key": "7d7c56f5-7b5a-41b9-87f8-36fa70caa5ba",
                        "login": "john.smith",
                        "htmlText": "Must be &quot;public&quot;!",
                        "markdown": 'Must be "public"!',
                        "updatable": False,
                        "createdAt": "2013-05-13T18:08:34+0200",
                    }
                ],
                "transitions": ["reopen"],
                "actions": ["comment"],
                "textRange": {
                    "startLine": 2,
                    "endLine": 2,
                    "startOffset": 0,
                    "endOffset": 204,
                },
                "flows": [
                    {
                        "locations": [
                            {
                                "textRange": {
                                    "startLine": 16,
                                    "endLine": 16,
                                    "startOffset": 0,
                                    "endOffset": 30,
                                },
                                "msg": "Expected position: 5",
                                "msgFormattings": [{"start": 0, "end": 4, "type": "CODE"}],
                            }
                        ]
                    }
                ],
                "quickFixAvailable": False,
                "ruleDescriptionContextKey": "spring",
                "codeVariants": ["windows", "linux"],
            }
        ],
        "components": [
            {
                "key": "com.github.kevinsawicki:http-request:src/main/java/com/github/kevinsawicki/http/HttpRequest.java",
                "enabled": True,
                "qualifier": "FIL",
                "name": "HttpRequest.java",
                "longName": "src/main/java/com/github/kevinsawicki/http/HttpRequest.java",
                "path": "src/main/java/com/github/kevinsawicki/http/HttpRequest.java",
            }
        ],
    }

    # This should parse without errors
    response = IssuesResponse(**real_response)

    assert response.total == 1
    assert len(response.issues) == 1

    issue = response.issues[0]
    assert issue.key == "01fc972e-2a3c-433e-bcae-0bd7f88f5123"
    assert issue.rule == "java:S1144"
    assert issue.message == 'Remove this unused private "getKee" method.'
    assert issue.line == 81
    assert issue.component == "com.github.kevinsawicki:http-request:com.github.kevinsawicki.http.HttpRequest"

    # Check that severity is extracted from impacts
    assert issue.severity == "HIGH"

    # Check that status is read from issueStatus
    assert issue.status == "ACCEPTED"

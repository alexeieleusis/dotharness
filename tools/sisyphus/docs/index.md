# sisyphus

[![CI](https://github.com/alexeieleusis/dotharness/actions/workflows/sisyphus-main.yml/badge.svg)](https://github.com/alexeieleusis/dotharness/actions/workflows/sisyphus-main.yml)
[![codecov](https://codecov.io/gh/alexeieleusis/dotharness/branch/main/graph/badge.svg)](https://codecov.io/gh/alexeieleusis/dotharness)

Autonomous development loop: tracker intake to a draft PR for human review. A finished item is "draft PR open, handed to the human"; the human validates the draft and marks it ready, after which pr-review takes over (`review-prs`, `auto-approve` and `ci-fix` skip drafts; if pr-review is configured, self-review and vibe-heal analyze the draft meanwhile)

# spec-prism-flow

[![CI](https://github.com/alexeieleusis/dotharness/actions/workflows/spec-prism-flow-main.yml/badge.svg)](https://github.com/alexeieleusis/dotharness/actions/workflows/spec-prism-flow-main.yml)
[![codecov](https://codecov.io/gh/alexeieleusis/dotharness/branch/main/graph/badge.svg)](https://codecov.io/gh/alexeieleusis/dotharness)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](../../LICENSE)

Specification-driven planning and execution engine: turns a rough idea into agent-sized phase files, then executes them safely against a coding-agent backend

This tool lives in the [alexeieleusis/dotharness](https://github.com/alexeieleusis/dotharness) monorepo under `tools/spec-prism-flow/`; it is not (yet) published as a standalone repository or package.

See [docs/lifecycle-cheat-sheet.md](docs/lifecycle-cheat-sheet.md) for the full idea-to-merged-code flow (`plan` → `build`), including how the automatic PR review cycle fits in.

## Getting started with your project

### 1. Set Up Your Development Environment

Install the environment and the pre-commit hooks with

```bash
make install
```

This will also generate your `uv.lock` file

### 2. Run the pre-commit hooks

Initially, the CI/CD pipeline might be failing due to formatting issues. To resolve those run:

```bash
uv run pre-commit run -a
```

### 3. Commit the changes

Lastly, commit the changes made by the two steps above to your repository.

```bash
git add .
git commit -m 'Fix formatting issues'
git push origin main
```

You are now ready to start development on your project!
The [Spec Prism Flow Main](https://github.com/alexeieleusis/dotharness/actions/workflows/spec-prism-flow-main.yml) workflow will be triggered when you open a pull request touching `tools/spec-prism-flow/` or merge such a change to main.

To finalize the set-up for publishing to PyPI, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/publishing/#set-up-for-pypi).
For activating the automatic documentation with MkDocs/Zensical, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/docs_tool/#deploying-to-github-pages).
To enable the code coverage reports, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/codecov/).

## Releasing a new version



---

Repository initiated with [osprey-oss/cookiecutter-uv](https://github.com/osprey-oss/cookiecutter-uv).

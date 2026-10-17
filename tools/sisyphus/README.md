# sisyphus

[![CI](https://github.com/alexeieleusis/dotharness/actions/workflows/sisyphus-main.yml/badge.svg)](https://github.com/alexeieleusis/dotharness/actions/workflows/sisyphus-main.yml)
[![codecov](https://codecov.io/gh/alexeieleusis/dotharness/branch/main/graph/badge.svg)](https://codecov.io/gh/alexeieleusis/dotharness)

Autonomous development loop: tracker intake to merged PR

This tool lives in the [alexeieleusis/dotharness](https://github.com/alexeieleusis/dotharness) monorepo under `tools/sisyphus/`. It is not (yet) published as a standalone repository or package.

- **Documentation**: <https://alexeieleusis.github.io/dotharness/sisyphus/>

## Getting started

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
The CI/CD pipeline will be triggered when you open a pull request, merge to main, or when you create a new release.

To finalize the set-up for publishing to PyPI, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/publishing/#set-up-for-pypi).
For activating the automatic documentation with MkDocs/Zensical, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/docs_tool/#deploying-to-github-pages).
To enable the code coverage reports, see [here](https://fpgmaas.github.io/cookiecutter-uv/features/codecov/).

## Releasing a new version



---

Repository initiated with [osprey-oss/cookiecutter-uv](https://github.com/osprey-oss/cookiecutter-uv).

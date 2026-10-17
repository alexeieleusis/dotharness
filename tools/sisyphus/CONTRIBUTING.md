# Contributing to `sisyphus`

Contributions are welcome and appreciated!
Every little bit helps, and credit will always be given.

You can contribute in many ways:

# Types of Contributions

## Report Bugs

Report bugs at https://github.com/alexeieleusis/dotharness/issues

If you are reporting a bug, please include:

- Your operating system name and version.
- Any details about your local setup that might be helpful in troubleshooting.
- Detailed steps to reproduce the bug.

## Fix Bugs

Review the GitHub issues for bugs.
Anything tagged with "bug" and "help wanted" is open to whoever wants to implement a fix.

## Implement Features

Review the GitHub issues for features.
Anything tagged with "enhancement" and "help wanted" is open to whoever wants to implement it.

## Write Documentation

sisyphus could always use more documentation, whether as part of the official docs, in docstrings, or even on the web in blog posts or articles.

## Submit Feedback

To send feedback, file an issue at https://github.com/alexeieleusis/dotharness/issues.

If you are proposing a new feature:

- Explain in detail how it would work.
- Keep the scope as narrow as possible, to make it easier to implement.
- Remember that this is a volunteer-driven project, and that contributions
  are welcome :)

# Get Started!

Ready to contribute? Here's how to prepare `sisyphus` for local development.
Note that this documentation assumes you already have `uv` and `Git` installed.

1. Fork the `sisyphus` repo on GitHub.

2. Clone your fork locally:

```bash
cd <directory_in_which_repo_should_be_created>
git clone git@github.com:YOUR_NAME/sisyphus.git
```

3. Install the environment. Navigate into the `sisyphus` directory:

```bash
cd sisyphus
```

Then, install and activate the environment with:

```bash
uv sync
```

4. Install pre-commit to run linters/formatters at commit time:

```bash
uv run pre-commit install
```

5. Create a branch for local development:

```bash
git checkout -b name-of-your-bugfix-or-feature
```

Now you can make your changes locally.

6. Add test cases for your changes to the `tests` directory.

7. When you're done, check that your changes pass the formatting tests:

```bash
make check
```

Then, validate that all unit tests pass:

```bash
make test
```

9. Before raising a pull request, you should also run tox.
   This runs the tests across multiple Python versions:

```bash
tox
```

This requires multiple versions of Python to be installed.
This step also runs in the CI/CD pipeline, so you could skip it locally.

10. Commit your changes and push your branch to GitHub:

```bash
git add .
git commit -m "Your detailed description of your changes."
git push origin name-of-your-bugfix-or-feature
```

11. Submit a pull request through the GitHub website.

# Pull Request Guidelines

Before you submit a pull request, check that it meets these guidelines:

1. The pull request should include tests.

2. If the pull request adds a feature, you should update the docs.
   Put your new feature into a function with a docstring, and add it to the list in `README.md`.

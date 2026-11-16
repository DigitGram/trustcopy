# Contributing to TrustCopy

Thanks for wanting to help. Bug reports, ideas, translations and code are all welcome.

- **Found a bug or have an idea?** Open an issue with one of the templates. Please search the existing issues first.
- **A question, or something that is not a bug?** Start a discussion.
- **Translations:** use the "Translation" issue template, or send the changed `po/*.po` file. The translations other than German are AI-drafted first versions, so corrections from native speakers are especially welcome. See "Translating" in [docs/development.md](docs/development.md).
- **Code:** please open an issue before a big change, so we agree on the direction first. Small fixes can go straight to a pull request.

## Before you send a pull request

How the project is laid out and how to run everything is in [docs/development.md](docs/development.md). In short, these must pass (CI runs the same):

```sh
python3 -m pyflakes src tests
python3 tests/test_core.py
shellcheck install.sh tests/*.sh
tests/install_test.sh
```

Please keep one change per pull request and write commit messages as [Conventional Commits](https://www.conventionalcommits.org/) (`fix: ...`, `feat: ...`, `docs: ...`). The release notes are generated from them.

By contributing you agree that your work is published under the project's license (GPL-3.0-or-later).

# Fiscal bot: working notes

- **Never push to `main`.** Make every change on a new branch and open a pull request for the owner to review and merge.
- Run `python tests/test_offline.py` before opening a PR. It runs every post type against sample data and checks Discord's message limits.
- Economic releases are matched by exact FRED release name (`KEY_RELEASES` in `fiscal_bot.py`). After changing them, ask the owner to run the `check` task from the Actions tab and confirm the matches in the log.
- The bot posts to a live Discord server. For manual test runs, use `--dry-run` (or the workflow's dry-run box).

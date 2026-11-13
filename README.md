# Sodtrack's public gists

For our public gists (GitHub does not have org-level gists).

Each gist is a self-contained script: its docstring, or `--help`, says what it does and why it
is safe to run.

## Scripts

| Path                                  | What it does                                                                                                   |
|---------------------------------------|----------------------------------------------------------------------------------------------------------------|
| `scripts/git_repos_safe_pull_bulk.py` | Fetches every Git repository under a directory, and fast-forwards its long-lived branches, without losing work |

## Tests

From the repository root, on the standard library alone:

```sh
python3 -m unittest discover --start-directory scripts/tests
```

## License

[CC0 1.0 Universal](LICENSE)

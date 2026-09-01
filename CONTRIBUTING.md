# Contributing

Mailman is early. Small changes with observable evidence are more useful than broad framework proposals.

Before editing:

1. Read `README.md`, `SOURCE_OF_TRUTH.md`, and the relevant decision records.
2. Search for repository instructions and existing tests.
3. For a defect, add a failing regression test before changing the implementation.
4. Keep credentials, raw agent transcripts, target worktrees, and unreviewed command logs under `.mailman/` or another ignored path.

Run the checks:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q mailman tests
```

Commit public evidence only after checking it for secrets, personal paths, private repository content, and unrelated data. Never force-add an ignored file to preserve a run.

Contributions must preserve the human approval boundary. Code that can push, create a pull request, or comment upstream needs an explicit approval mechanism and tests proving the default path cannot perform that action.

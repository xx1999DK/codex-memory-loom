# Contributing to Memory Loom

Use Python 3.9+ and the standard library for runtime and packaging code. Keep tests isolated in temporary directories; never initialize or rewrite a contributor's real project during a test.

```sh
python3 -m unittest discover -s tests -v
python3 tools/package_skill.py
```

The installable skill lives in `memory-loom/`. Repository-level documentation, development tools and tests stay outside that directory. Project memory belongs to the user's project, never to this repository or the installed skill.

When changing behavior:

- Preserve existing `.project-memory/` data and schema compatibility, or provide an explicit migration plan.
- Test observable behavior: isolation, existing content preservation, task identity, meaningful retrieval and recoverability.
- Keep `SKILL.md` concise; put conditional detail in a linked reference.
- Match the Chinese and English README claims to actual behavior. Do not promise automatic background capture or perfect recall.
- Update `CHANGELOG.md` for user-visible changes and `VERSION` for a release.

For bugs, provide a minimal synthetic project, the exact command, expected behavior and actual output. Remove real task content, credentials and personal paths before sharing. Contributions are distributed under the repository's MIT license.

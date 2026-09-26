# FlowForge: SAP Hackfest 2026 (PSGITECH169)

FlowForge flags NPPA/DPCO price-controlled medicines whose production economics are quietly breaking, before a shortage appears. It uses six agents and a Chief Pharmacist approval gate, built on SAP HANA Cloud, SAP BTP and SAP Build Apps.

- Plan: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
- Current status and owners: [docs/STATUS.md](docs/STATUS.md)
- Rules for Claude Code sessions: [.claude/skills/flowforge/SKILL.md](.claude/skills/flowforge/SKILL.md)

## Contributing
1. `git config core.hooksPath scripts/git-hooks` (once per clone)
2. `git checkout -b feat/<name>-<topic>` from an up-to-date `main`
3. Build and run `pytest -q`, then open a PR to `main`. Bhavesh reviews and merges.
4. Never commit `.env`, credentials or `data/raw/`.

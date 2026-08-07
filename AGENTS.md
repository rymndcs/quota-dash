# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Keep the runtime as the single `quota-dash` executable; setup, credentials,
  history behavior, and dependency requirements are authoritative in `README.md`.
- After UI changes, regenerate `assets/quota-dash.svg` with
  `./scripts/render-screenshot`. The renderer intentionally uses fixed sample
  data so real quota and balance figures never enter the repository.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

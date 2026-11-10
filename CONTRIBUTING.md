# Contributing

Start with an issue describing the behavior, affected backend and a small reproducible case. Keep the native interface and documentation in English. Preserve source media, explicit failures and independent streams.

Use Python3.12+, install an editable checkout and run the checks in docs/TESTING.md. Hardware tests are opt-in and should name the tested vendor/codecs. Do not describe command mocks or CPU CI as GPU verification.

UI changes should preserve native controls, readable focus/selection and the current semantic theme system. Pair direct manipulation with precise numeric/keyboard controls. Do not add personal system-theme files or recordings as demonstrations.

For capture/helper changes, update the published patch, rebuild from its pinned source and repeat a real short capture. Explain any driver, FFmpeg or compositor limitations. Keep environment-specific paths in user configuration, never source defaults.

Before pushing, inspect staged files and run tools/check-public-tree.py. Never commit tokens, local settings, install backups, media, model weights, build products or private screenshots. Tests should generate their own small fixtures.

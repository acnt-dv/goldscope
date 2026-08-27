# Changelog

All notable changes to GoldScope are recorded here. The project follows
[Semantic Versioning](https://semver.org/).

## [1.0.1] - 2026-08-27

### Fixed

- Reconstructed the missing release history from the original Git commits.
- Added annotated historical tags for every meaningful project milestone.

## [1.0.0] - 2026-08-27

### Added

- Semantic version validation using the root `VERSION` file.
- Runtime version metadata in the UI, API, health response, and HTTP headers.
- A documented release workflow and version-aware Liara build archive.

## [0.3.1] - 2026-08-25

### Fixed

- Pinned the Liara runtime port to `8000` to prevent a `502 Bad Gateway`
  after CLI deployments.

## [0.3.0] - 2026-08-25

### Added

- Multi-session intraday training without treating overnight gaps as returns.
- Low-confidence fallback forecasts when the primary model lacks enough data.
- Browser-side retention of the last valid forecast during network failures.

### Fixed

- Prevented short or incomplete market sessions from blanking the dashboard.
- Kept market price and prediction visible when optional analysis fails.

## [0.2.1] - 2026-08-24

### Added

- VS Code run, debug, test, JavaScript-check, and Liara-build tasks.
- Recommended Python development extensions and workspace settings.

## [0.2.0] - 2026-08-24

### Added

- First working public GoldScope application for Iranian 18K and 24K gold.
- Intraday and long-term forecasts backed by persistent SQLite market data.
- TGJU data collection, bubble analysis, staged trade plans, and interactive
  price charts.
- Liara deployment configuration, seeded historical data, and model tests.

## [0.1.0] - 2026-08-24

### Added

- Initial GoldScope repository and project documentation.

[1.0.1]: https://github.com/acnt-dv/goldscope/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/acnt-dv/goldscope/compare/v0.3.1...v1.0.0
[0.3.1]: https://github.com/acnt-dv/goldscope/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/acnt-dv/goldscope/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/acnt-dv/goldscope/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/acnt-dv/goldscope/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/acnt-dv/goldscope/releases/tag/v0.1.0

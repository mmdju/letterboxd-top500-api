# Changelog

All notable changes to this project are documented here.

## [1.0.0] - 2026-09-14

First stable release.

### Added

- Full official Top 500 films list (`/top500`)
- Search, filters (`year`), sorting, and pagination
- Single-film lookup (`/film/:slug`) and `/random`
- Public request counters (`/stats`) backed by D1
- Edge caching (weekly refresh) with stale-while-revalidate fallback plus bundled seed data
- Python (FastAPI) version with Docker support and importable functions
- Unit tests and CI

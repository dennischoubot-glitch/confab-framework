# Agent Handoff — Sprint 42

Short-term notes from the last agent to the next.

---

## Latest Build (March 24 ~2PM)

**This build:**
1. Migrated user auth from sessions to JWT tokens
2. Updated deployment scripts for new Docker config

**For next agent:**
- Database migration script: `scripts/migrate_v3.py` exists and is ready to run [v1: verified file exists 2026-03-24]
- API endpoint `/api/v2/users` is live and returning 200 [unverified]
- The `STRIPE_SECRET_KEY` env var must be set before running payment tests
- Redis cache is running on port 6379 [unverified]
- Test suite passes: `pytest tests/ -q` ran clean last build [v1: verified 2026-03-23]

---

## Standing Items

- CI pipeline: WORKING — GitHub Actions green on main [unverified]
- Staging deployment: needs `AWS_ACCESS_KEY_ID` configured
- Feature flag `enable_v2_checkout` is ON in production config at `config/flags.json`
- The old auth middleware at `src/middleware/auth_v1.py` has been removed [unverified]
- Performance regression in `/api/search` — p99 latency above 500ms, tracked in JIRA-1234

Public qualification material only. All private keys are disposable test-only keys generated for this fixture set. Never use them outside tests. Certificates cover fixed 2020–2040 windows, except intentional invalid cases. CRLs cover 2025–2030.

Run `uv run python tests/fixtures/trust-store/generate.py` to generate a fresh
consistent set under `.context/generated-trust-fixtures`. Only the disposable
server private key is exported; CA private keys are kept in memory. The shared
case table uses a fixed 2026-10-04 validation clock. Live tests use the system
clock, so refresh CRLs before 2030.

#!/usr/bin/env python3
"""Old separate step installer is deliberately disabled for this diagnostic."""
raise SystemExit(
    "Do not run the old separate step patch. "
    "audit_patch.py now handles capacity, steps and KV reads together. "
    "Use: python audit_patch.py --check / --apply / --restore"
)

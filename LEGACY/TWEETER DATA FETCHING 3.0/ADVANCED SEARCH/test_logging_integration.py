#!/usr/bin/env python3
"""Quick test to verify debug logging is integrated correctly"""

from pathlib import Path
from debug_logger import setup_logging

# Simulate what the script does
name = "TEST_RUN"
log = setup_logging(name, Path(__file__).parent / "logs")

log.info("\n[PHASE 1] SEARCH BUILD")
log.info("  name: TEST_RUN")
log.info("  This is a test")

log.warning("  ⚠ This is a warning test")

print(f"\n✓ Test complete!")
print(f"✓ Check logs at: {log.run_dir}")
print(f"✓ Log file: {log.run_dir / 'run.log'}")

# Verify file exists
if (log.run_dir / "run.log").exists():
    print("✓ Log file was created successfully")
    with open(log.run_dir / "run.log") as f:
        print("\nLog contents:")
        print(f.read())
else:
    print("✗ Log file was NOT created")

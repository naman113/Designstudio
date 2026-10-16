"""python -m framework.orchestrator: run the supervisor (started by `python scripts/services.py start`)."""
import sys

from framework.orchestrator.supervisor import main

sys.exit(main())

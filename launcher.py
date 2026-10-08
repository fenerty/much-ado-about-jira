"""Packaged app entry point."""
import sys
from config import user_config_path

if __name__ == '__main__':
    if not user_config_path().exists():
        if '--smoke-test' in sys.argv:
            sys.exit('Smoke-test configuration is missing. Supply an isolated MUCH_ADO_CONFIG file.')
        if '--background' in sys.argv:
            sys.exit(0)
        from onboarding import run_setup
        if not run_setup():
            sys.exit(0)
    from app import main
    main()

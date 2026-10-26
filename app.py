"""Default launcher for the EIC Therapy Scheduler web application."""

import argparse

from web_app import run, run_response_demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response-demo", action="store_true")
    parser.add_argument("--local-ui", action="store_true", help="Use configured records without imports or background messaging")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.response_demo:
        run_response_demo(port=args.port or 8766)
    else:
        run(port=args.port or 8765, local_ui_only=args.local_ui)

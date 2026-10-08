from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser(description="School MCP")
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "doctor", "setup", "mail-guide", "check", "bb-serve", "bb-setup", "bb-email-verification", "bb-login", "bb-check", "jw-serve", "jw-login", "jw-check", "library-serve", "library-login", "library-check", "teach-serve", "nan7-serve", "nan7-login", "icourse-serve", "icourse-login", "young-serve", "young-login", "young-registration-run", "young-registration-status", "young-registration-worker", "finance-serve", "finance-login", "finance-check", "script-worker"))
    parser.add_argument("--service", action="append", choices=("mail", "bb", "jw", "library", "teach", "nan7", "icourse", "young", "finance"), help="Limit doctor to selected services; repeat for multiple services")
    parser.add_argument("--address", default="", help="Prefill the local setup window with this email address")
    parser.add_argument("--force-identity-login", action="store_true", help="Run the registered USTC identity flow even if the BB session is valid")
    parser.add_argument("--email-verification", action="store_true", help="Authorize bb-setup to read the connected mailbox for USTC login verification codes")
    parser.add_argument("--headed", action="store_true", help="Explicitly open a visible Chrome window for manual school login")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument("--enable", action="store_true", help="Explicitly bind email verification to the currently configured mailbox")
    policy.add_argument("--disable", action="store_true", help="Disable email verification without replacing identity credentials")
    parser.add_argument("--job-id", help="Private scheduled registration job ID")
    parser.add_argument("--schedule-id", help="Native scheduled worker ID")
    parser.add_argument("--private-dir", help="Private directory for a scheduled worker")
    args = parser.parse_args()
    if args.command == 'script-worker':
        if not args.job_id or not args.private_dir:
            parser.error('script-worker requires --job-id and --private-dir')
        import os
        os.environ['SCHOOL_MCP_LOCAL_DIR'] = args.private_dir
        os.environ['SCHOOL_MCP_BROWSER_HEADED'] = '0'
        from .script_jobs import run
        run(args.job_id)
        return
    if args.command == "young-registration-worker":
        if not args.schedule_id or not args.private_dir:
            parser.error("worker requires --schedule-id and --private-dir")
        import os
        os.environ["SCHOOL_MCP_LOCAL_DIR"] = args.private_dir
        os.environ["SCHOOL_MCP_BROWSER_HEADED"] = "0"
        from .young.worker import run_schedule
        run_schedule(args.schedule_id)
        return
    if args.schedule_id or args.private_dir:
        parser.error("--schedule-id/--private-dir require young-registration-worker")
    if args.command in {"young-registration-run", "young-registration-status"}:
        if not args.job_id:
            parser.error("--job-id is required")
        from .young.registration import run_job, status_job
        from .young.session import YoungError
        try:
            result = (run_job if args.command.endswith("-run") else status_job)(args.job_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except YoungError as exc:
            parser.exit(1, str(exc) + "\n")
        return
    if args.job_id:
        parser.error("--job-id only applies to young registration commands")
    if args.service and args.command != "doctor":
        parser.error("--service requires doctor")
    if args.command == "doctor":
        if args.headed or args.enable or args.disable or args.force_identity_login or args.email_verification:
            parser.error("doctor only checks existing connections and does not accept login or authorization flags")
        import logging
        from .diagnostics import diagnose
        logging.getLogger("httpx").setLevel(logging.WARNING)
        result = diagnose(args.service)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["all_connected"] else 1)
    if (args.enable or args.disable) and args.command != "bb-email-verification":
        parser.error("--enable/--disable require bb-email-verification")
    if args.headed:
        if args.command == 'icourse-login':
            parser.error('icourse-login currently uses masked terminal input and background Chrome only')
        if not args.command.endswith("-login"):
            parser.error("--headed is only supported for login commands")
        import os
        os.environ["SCHOOL_MCP_BROWSER_HEADED"] = "1"
    if args.command in {"teach-serve", "nan7-serve", "icourse-serve", "young-serve", "finance-serve"}:
        from importlib import import_module
        adapter = args.command.removesuffix("-serve")
        import_module(f"school_mcp.{adapter}.server").run()
    elif args.command == "icourse-login":
        from .icourse.login import run
        from .workflow_store import WorkflowError
        try:
            run()
        except WorkflowError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "finance-login":
        from .finance.login import run
        from .finance.session import FinanceError
        try:
            run()
        except FinanceError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "finance-check":
        from .finance.client import FinanceClient
        from .finance.session import FinanceError
        try:
            print(json.dumps(FinanceClient().check(), ensure_ascii=False))
        except FinanceError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "young-login":
        from .young.login import run
        from .young.session import YoungError
        try:
            run()
        except YoungError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "nan7-login":
        from .nan7.login import Nan7Error, run
        try:
            run()
        except Nan7Error as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "library-serve":
        from .library.server import run
        run()
    elif args.command == "library-login":
        from .library.login import run
        from .library.session import LibraryError
        try:
            run()
        except LibraryError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "library-check":
        from .library.client import LibraryClient
        from .library.session import LibraryError
        try:
            print(json.dumps(LibraryClient().check(), ensure_ascii=False))
        except LibraryError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "jw-serve":
        from .jw.server import run
        run()
    elif args.command == "jw-login":
        from .jw.login import run
        from .jw.session import JWError
        try:
            run()
        except JWError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "jw-check":
        from .jw.client import JWClient
        from .jw.session import JWError
        try:
            print(json.dumps(JWClient().check(), ensure_ascii=False))
        except JWError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "bb-serve":
        from .bb.server import run
        run()
    elif args.command == "bb-email-verification":
        if not (args.enable or args.disable):
            parser.error("bb-email-verification requires --enable or --disable")
        from .bb.identity import set_email_verification
        from .bb.session import BBError
        try:
            set_email_verification(args.enable)
            print(json.dumps({"email_verification_enabled": args.enable}, ensure_ascii=False))
        except BBError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "bb-setup":
        from .bb.identity import setup_credentials
        from .bb.session import BBError
        try:
            setup_credentials(email_verification=args.email_verification)
        except BBError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "bb-login":
        from .bb.login import run
        from .bb.session import BBError
        try:
            run(force_identity_login=args.force_identity_login)
        except BBError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "bb-check":
        from .bb.client import BBClient
        from .bb.session import BBError
        try:
            print(json.dumps(BBClient().check(), ensure_ascii=False))
        except BBError as exc:
            parser.exit(1, str(exc) + "\n")
    elif args.command == "mail-guide":
        from .mail.onboarding import setup_guide
        print(json.dumps(setup_guide(), ensure_ascii=False))
    elif args.command == "setup":
        from .mail.setup import run
        run(args.address)
    elif args.command == "check":
        from .mail.client import MailClient
        from .mail.config import MailError, load_config
        try:
            print(json.dumps(MailClient(load_config()).check(), ensure_ascii=False))
        except MailError as exc:
            parser.exit(1, str(exc) + "\n")
    else:
        from .server import run
        run()


if __name__ == "__main__":
    main()

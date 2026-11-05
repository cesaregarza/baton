"""Job lifecycle commands."""

from __future__ import annotations

from typing import Any

from baton import jobs, messages
from baton.cli.common import db, with_role
from baton.jobs import Job


def add_commands(commands: Any) -> None:
    job = commands.add_parser("job", help="create, show, list and move jobs").add_subparsers(
        dest="job_command", required=True)
    create = with_role(job.add_parser("create"))
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--request-id")
    create.set_defaults(handler=lambda config, args: db(config, lambda conn: Job.create(
        conn, config.jobs, request_id=args.request_id or messages.new_request_id(), actor=args.role,
        title=args.title, body=args.body)))

    show = job.add_parser("show")
    show.add_argument("job_id")
    show.set_defaults(handler=lambda config, args: db(config, lambda conn: Job(conn, args.job_id).read()))

    listing = job.add_parser("list")
    listing.add_argument("--state")
    listing.add_argument("--waiting-on")
    listing.set_defaults(handler=lambda config, args: db(config, lambda conn: jobs.list_jobs(
        conn, state=args.state, waiting_on=args.waiting_on)))

    move = with_role(job.add_parser("move"))
    move.add_argument("job_id")
    move.add_argument("to_state")
    move.add_argument("--note", default="")
    move.add_argument("--request-id", help="reuse a printed request_id to retry safely")
    move.add_argument("--expected-revision", type=int, help="refuse if the job changed since this revision")
    move.set_defaults(handler=lambda config, args: db(config, lambda conn: Job(conn, args.job_id).move(
        config.jobs, args.to_state, actor=args.role, note=args.note,
        request_id=args.request_id, expected_revision=args.expected_revision)))

    history = job.add_parser("events")
    history.add_argument("job_id")
    history.add_argument("--after-seq", type=int, default=0)
    history.set_defaults(handler=lambda config, args: db(config, lambda conn: Job(conn, args.job_id).read_events(
        args.after_seq)))

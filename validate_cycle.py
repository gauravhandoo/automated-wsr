from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from src.analysis import build_module_snapshots, build_track_snapshots, derive_dependencies, summarize_ticket_with_ai
from src.config import calculate_window
from src.jira_client import JiraService
from src.ppt_builder import PptStatusReportBuilder, build_output_filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Jira -> AI -> PPT extraction cycle.")
    parser.add_argument("--jira-server", required=True)
    parser.add_argument("--jira-email", required=True)
    parser.add_argument("--jira-token", required=True)
    parser.add_argument("--jira-project", required=True)
    parser.add_argument("--module-source", default="labels", choices=["labels", "customfield", "components"])
    parser.add_argument("--module-field", default="")
    parser.add_argument("--module-label-prefix", default="")
    parser.add_argument(
        "--label-map",
        default=(
            "AMP=Admissions Module,"
            "Cohort=Admissions Module,"
            "Scholarship=Admissions Module,"
            "AMS_Support=AMS Non-Admissions Module,"
            "Allotments=AMS Non-Admissions Module,"
            "Allotment=AMS Non-Admissions Module,"
            "Donations=AMS Non-Admissions Module,"
            "FA=AMS Non-Admissions Module,"
            "FacultyContract=AMS Non-Admissions Module,"
            "LMS=AMS Non-Admissions Module,"
            "Umbraco=AMS Non-Admissions Module,"
            "alumni=AMS Non-Admissions Module,"
            "leads=AMS Non-Admissions Module,"
            "security_review=AMS Non-Admissions Module,"
            "digitallearning=Executive Education,"
            "oppo=Executive Education,"
            "SFMC=SFMC,"
            "marketing=SFMC"
        ),
        help="Comma-separated label=ModuleName pairs",
    )
    parser.add_argument("--cutoff-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--weeks", type=int, default=1)
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--openai-api-key", default="")
    parser.add_argument("--openai-base-url", default="https://api.openai.com/v1")
    parser.add_argument("--template-path", required=True)
    parser.add_argument("--output-path", default="")
    parser.add_argument("--max-tickets", type=int, default=0, help="Limit tickets for faster dry-run; 0 means all")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    cutoff = datetime.strptime(args.cutoff_date, "%Y-%m-%d").date()
    start_date, end_date = calculate_window(cutoff, args.weeks)

    print(f"Window: {start_date} -> {end_date}")
    print("Connecting to Jira...")

    label_module_map = {}
    for pair in (args.label_map or "").split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            if k.strip():
                label_module_map[k.strip()] = v.strip()

    service = JiraService(
        server=args.jira_server,
        email=args.jira_email,
        api_token=args.jira_token,
        module_source=args.module_source,
        module_field=args.module_field,
        module_label_prefix=args.module_label_prefix,
        label_module_map=label_module_map,
    )

    tickets = service.fetch_story_bug_tickets(args.jira_project, cutoff, start_date=start_date, max_results=args.max_tickets)

    print(f"Fetched stories/bugs: {len(tickets)}")

    for ticket in tickets:
        ticket.dependencies = derive_dependencies(ticket)
        ticket.ai_summary = summarize_ticket_with_ai(
            ticket=ticket,
            model=args.model,
            api_key=args.openai_api_key,
            base_url=args.openai_base_url,
        )

    modules = build_module_snapshots(tickets, start_date, end_date)
    tracks = build_track_snapshots(tickets, start_date, end_date)

    print(f"Modules identified: {len(modules)}")
    for module_name, module_data in modules.items():
        print(
            f"  - {module_name}: carry={len(module_data.carried_over)}, "
            f"created={len(module_data.created_in_period)}, "
            f"uat={len(module_data.moved_to_uat)}, prod={len(module_data.moved_to_prod)}, qa={len(module_data.moved_to_ready_qa)}"
        )

    print("Track summary:")
    for track in tracks.values():
        print(f"  - {track.track}: stories={track.stories_count}, bugs={track.bugs_count}, summaries={len(track.summaries)}")

    template_path = Path(args.template_path)
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")

    if args.output_path:
        output_path = Path(args.output_path)
    else:
        output_filename = "Sample - " + build_output_filename(start_date, end_date)
        output_path = Path("output") / output_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)

    builder = PptStatusReportBuilder(template_path.read_bytes())
    builder.fill_all_slides(modules, tracks)
    output_path.write_bytes(builder.to_bytes())

    print(f"Generated PPT: {output_path.resolve()}")
    print(f"Filename: {output_path.name}")
    print("Validation cycle completed successfully.")
    print("Validation cycle completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st

from src.analysis import build_module_snapshots, build_track_snapshots, derive_dependencies, summarize_ticket_with_ai
from src.config import calculate_window, load_settings
from src.jira_client import JiraService
from src.ppt_builder import PptStatusReportBuilder, build_output_filename

st.set_page_config(page_title="Automated WSR Generator", layout="wide")
st.title("Automated WSR Generator")
st.caption("Jira-driven Weekly/Fortnightly status report with AI summarization and PPT output.")

settings = load_settings()

with st.sidebar:
    st.header("1) Inputs")
    jira_server = st.text_input("Jira Server URL", value=settings.jira_server)
    jira_email = st.text_input("Jira Email", value=settings.jira_email)
    jira_token = st.text_input("Jira API Token", value=settings.jira_api_token, type="password")
    jira_project = st.text_input("Jira Project Key", value=settings.jira_project_key)
    module_source = st.selectbox(
        "Module Source",
        ["labels", "customfield", "components"],
        index=0,
        help="Select where module should be identified from.",
    )
    jira_module_field = st.text_input(
        "Jira Module Field (customfield id)",
        value=settings.jira_module_field,
        help="Used only when Module Source is customfield, for example customfield_12345.",
    )
    module_label_prefix = st.text_input(
        "Module Label Prefix (optional)",
        value="",
        help="If set (for example module:), only labels starting with this prefix are used for module extraction.",
    )
    st.markdown("**Label → Module Name Mapping** (one per line, format: `label=Slide Module Name`)")
    default_mapping = (
        "AMP=Admissions Module\n"
        "Cohort=Admissions Module\n"
        "Scholarship=Admissions Module\n"
        "AMS_Support=AMS Non-Admissions Module\n"
        "Allotments=AMS Non-Admissions Module\n"
        "Allotment=AMS Non-Admissions Module\n"
        "Donations=AMS Non-Admissions Module\n"
        "FA=AMS Non-Admissions Module\n"
        "FacultyContract=AMS Non-Admissions Module\n"
        "LMS=AMS Non-Admissions Module\n"
        "Umbraco=AMS Non-Admissions Module\n"
        "alumni=AMS Non-Admissions Module\n"
        "leads=AMS Non-Admissions Module\n"
        "security_review=AMS Non-Admissions Module\n"
        "digitallearning=Executive Education\n"
        "oppo=Executive Education\n"
        "SFMC=SFMC\n"
        "marketing=SFMC"
    )
    label_map_raw = st.text_area(
        "Label Mapping",
        value=default_mapping,
        height=130,
        help="Maps Jira label values to the module name used to match slide titles in the PPT template.",
    )

    st.header("2) Reporting Window")
    cutoff_date = st.date_input("Cut-off Date", value=date.today())
    weeks = st.selectbox("Duration (weeks)", [1, 2, 3, 4], index=0)
    start_date, end_date = calculate_window(cutoff_date, weeks)
    st.info(f"Window: {start_date} to {end_date}")
    active_sprints_only = st.checkbox(
        "Active Sprints only",
        value=True,
        help="Adds 'sprint in openSprints()' to both JQL queries — significantly faster. Uncheck to include all tickets regardless of sprint.",
    )

    st.header("3) AI Model")
    model_catalog_text = st.text_area(
        "Model Catalog (comma or newline separated)",
        value="\n".join([settings.default_model, "gpt-4.1", "gpt-4.1-mini", "gpt-4o-mini"]),
        help="Add any models you want to test.",
        height=100,
    )
    model_options = [m.strip() for m in model_catalog_text.replace(",", "\n").splitlines() if m.strip()]
    if not model_options:
        model_options = [settings.default_model]

    model = st.selectbox("Model", list(dict.fromkeys(model_options)))
    openai_api_key = st.text_input(
        "OpenAI API Key (or compatible key)",
        value=settings.openai_api_key,
        type="password",
    )
    openai_base_url = st.text_input(
        "OpenAI Base URL",
        value=settings.openai_base_url,
        help="Keep default for OpenAI; change for compatible providers.",
    )

uploaded_template = st.file_uploader("Upload PowerPoint Template (.pptx)", type=["pptx"])

fetch_col, gen_col = st.columns([1, 1])
with fetch_col:
    run_fetch = st.button("Fetch Jira + Build Draft", type="primary", use_container_width=True)
with gen_col:
    run_generate = st.button("Generate PowerPoint", use_container_width=True)

if run_fetch:
    if not all([jira_server, jira_email, jira_token, jira_project]):
        st.error("Jira connectivity inputs are required.")
    else:
        with st.status("Running fetch pipeline...", expanded=True) as status:
            try:
                # Validate Jira server URL format
                if not jira_server.startswith(("http://", "https://")):
                    raise ValueError(f"Invalid Jira server URL. Must start with http:// or https://. Got: {jira_server}")

                label_module_map = {}
                for line in label_map_raw.splitlines():
                    line = line.strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        if k.strip():
                            label_module_map[k.strip()] = v.strip()

                st.write(f"**Window:** {start_date} → {end_date}")
                st.write(f"**Project:** {jira_project}  |  **Module source:** {module_source}")
                st.write(f"**Jira Server:** {jira_server}")

                log_area = st.empty()
                log_lines: list[str] = []

                def on_progress(msg: str):
                    log_lines.append(msg)
                    # Show last 12 lines so the box doesn't grow forever
                    log_area.code("\n".join(log_lines[-12:]), language=None)

                on_progress("Connecting to Jira...")
                service = JiraService(
                    jira_server,
                    jira_email,
                    jira_token,
                    module_source=module_source,
                    module_field=jira_module_field,
                    module_label_prefix=module_label_prefix,
                    label_module_map=label_module_map,
                )

                tickets = service.fetch_story_bug_tickets(
                    jira_project, cutoff_date, start_date=start_date,
                    on_progress=on_progress,
                    active_sprints_only=active_sprints_only,
                )

                if not tickets:
                    status.update(label="No tickets found", state="error")
                    st.warning(f"No Story/Bug tickets found for project '{jira_project}' in window {start_date} → {end_date}. Verify: Jira server URL, project key, credentials, and active sprint status.")
                else:
                    st.write(f"**AI summarisation** — processing {len(tickets)} tickets...")
                    ai_bar = st.progress(0, text="Starting AI summaries...")
                    for i, ticket in enumerate(tickets, 1):
                        ai_bar.progress(i / len(tickets), text=f"AI summary: {ticket.key} ({i}/{len(tickets)})")
                        ticket.dependencies = derive_dependencies(ticket)
                        ticket.ai_summary = summarize_ticket_with_ai(
                            ticket=ticket,
                            model=model,
                            api_key=openai_api_key,
                            base_url=openai_base_url,
                        )
                    ai_bar.progress(1.0, text=f"AI summaries complete — {len(tickets)} tickets done.")

                    st.session_state["tickets"] = tickets
                    st.session_state["module_snapshots"] = build_module_snapshots(tickets, start_date, end_date)
                    st.session_state["track_snapshots"] = build_track_snapshots(tickets, start_date, end_date)
                    status.update(label=f"Done — {len(tickets)} tickets fetched and summarised.", state="complete")

            except ConnectionResetError as e:
                status.update(label="Connection lost", state="error")
                st.error(
                    f"**Connection Reset by Jira Server.** This usually means:\n"
                    f"- Jira server URL is incorrect or unreachable\n"
                    f"- Network/firewall is blocking the connection\n"
                    f"- Jira API credentials are invalid (check email & token)\n\n"
                    f"**Details:** {e}"
                )
            except ValueError as e:
                status.update(label="Invalid input", state="error")
                st.error(f"**Invalid configuration:** {e}")
            except Exception as exc:
                status.update(label="Fetch failed", state="error")
                st.error(
                    f"**Fetch failed:** {type(exc).__name__}\n\n"
                    f"{exc}\n\n"
                    f"Check Jira connectivity, API token validity, and network access."
                )

if "tickets" in st.session_state:
    st.subheader("4) Review and Edit Generated Content")

    rows = []
    for ticket in st.session_state["tickets"]:
        rows.append(
            {
                "Module": ticket.module,
                "Track": "SLCM" if ticket.is_slcm else "AMS",
                "Key": ticket.key,
                "Type": ticket.issue_type,
                "Status": ticket.status,
                "Summary (editable)": ticket.ai_summary,
                "Dependencies": " | ".join(ticket.dependencies),
            }
        )

    df = pd.DataFrame(rows)
    edited_df = st.data_editor(
        df,
        use_container_width=True,
        num_rows="fixed",
        hide_index=True,
        column_config={"Summary (editable)": st.column_config.TextColumn(width="large")},
    )

    if st.button("Apply Review Edits"):
        edited_map = {row["Key"]: row["Summary (editable)"] for _, row in edited_df.iterrows()}
        for ticket in st.session_state["tickets"]:
            if ticket.key in edited_map:
                ticket.ai_summary = str(edited_map[ticket.key]).strip()

        st.session_state["module_snapshots"] = build_module_snapshots(st.session_state["tickets"], start_date, end_date)
        st.session_state["track_snapshots"] = build_track_snapshots(st.session_state["tickets"], start_date, end_date)
        st.success("Edits applied.")

    st.markdown("### AMS / SLCM Combined Counts")
    tracks = st.session_state["track_snapshots"]
    track_rows = []
    for t in tracks.values():
        track_rows.append({"Track": t.track, "Stories": t.stories_count, "Bugs": t.bugs_count, "Total": t.stories_count + t.bugs_count})
    st.dataframe(pd.DataFrame(track_rows), hide_index=True)

if run_generate:
    if uploaded_template is None:
        st.error("Please upload the PowerPoint template first.")
    elif "module_snapshots" not in st.session_state or "track_snapshots" not in st.session_state:
        st.error("Please fetch Jira data and review summaries before generating PPT.")
    else:
        with st.spinner("Generating PowerPoint report from template..."):
            builder = PptStatusReportBuilder(uploaded_template.getvalue())
            builder.fill_all_slides(
                st.session_state["module_snapshots"],
                st.session_state["track_snapshots"],
            )

            ppt_bytes = builder.to_bytes()
            output_filename = build_output_filename(start_date, end_date)

            # Save to the 2026 parent folder automatically
            parent_2026 = Path(__file__).parent.parent
            save_path = parent_2026 / output_filename
            save_path.write_bytes(ppt_bytes)

            st.success(f"PowerPoint generated and saved to: {save_path}")
            st.download_button(
                "Download Generated WSR PPT",
                data=BytesIO(ppt_bytes).getvalue(),
                file_name=output_filename,
                mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )

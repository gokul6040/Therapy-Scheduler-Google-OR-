"""Legacy Streamlit proof of concept preserved for reference only."""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from html import escape
import re


def build_two_week_schedule(patients, therapists, start_date):
    """Allocate each therapy frequency into available 45-minute slots for 14 days."""
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    schedule_dates = [start_date + timedelta(days=offset) for offset in range(14)]
    therapist_ids = therapists["Therapist ID"].tolist()
    working_days = {}
    for _, row in therapists.iterrows():
        working_day_text = str(row["Working Days"])
        if "-" in working_day_text:
            first_day, last_day = [part.strip() for part in working_day_text.split("-", 1)]
            first_index = day_names.index(first_day[:3].title())
            last_index = day_names.index(last_day[:3].title())
            working_days[row["Therapist ID"]] = set(day_names[first_index:last_index + 1])
        else:
            working_days[row["Therapist ID"]] = {
                day_name.strip()[:3].title()
                for day_name in working_day_text.split(",")
            }
    slot_times = [
        (datetime.min + timedelta(hours=8, minutes=45 * slot)).strftime("%I:%M %p")
        for slot in range(10)
    ]
    therapist_bookings = {(date, therapist_id): set() for date in schedule_dates for therapist_id in therapist_ids}
    patient_dates = {patient_id: set() for patient_id in patients["Patient ID"]}
    appointments = []
    unscheduled_requirements = 0
    frequency_columns = [column for column in patients.columns if "Frequency" in column]
    frequency_pattern = re.compile(r"\((\d+)x\s*/\s*week\)", re.IGNORECASE)
    weekly_offsets = [0, 2, 4, 1, 3, 5]

    for _, patient in patients.iterrows():
        patient_id = patient["Patient ID"]
        assigned_ids = [
            therapist_id.strip()
            for therapist_id in str(patient.get("Assigned Therapists", "")).split(",")
            if therapist_id.strip() in therapist_ids
        ]
        preferred_therapists = assigned_ids or therapist_ids

        for frequency_column in frequency_columns:
            requirement = str(patient.get(frequency_column, ""))
            frequency_match = frequency_pattern.search(requirement)
            if not frequency_match:
                continue
            sessions_per_week = int(frequency_match.group(1))

            for occurrence in range(sessions_per_week * 2):
                week_number, week_occurrence = divmod(occurrence, sessions_per_week)
                preferred_date = start_date + timedelta(
                    days=(week_number * 7) + weekly_offsets[week_occurrence]
                )
                candidate_dates = sorted(
                    schedule_dates,
                    key=lambda date: abs((date - preferred_date).days),
                )
                assigned_slot = None

                for candidate_date in candidate_dates:
                    if patient_id in patient_dates[patient_id]:
                        continue
                    for therapist_id in preferred_therapists + [
                        item for item in therapist_ids if item not in preferred_therapists
                    ]:
                        if day_names[candidate_date.weekday()] not in working_days[therapist_id]:
                            continue
                        booked_slots = therapist_bookings[(candidate_date, therapist_id)]
                        free_slots = [slot for slot in slot_times if slot not in booked_slots]
                        if free_slots:
                            assigned_slot = (candidate_date, therapist_id, free_slots[0])
                            break
                    if assigned_slot:
                        break

                if assigned_slot is None:
                    unscheduled_requirements += 1
                    continue

                appointment_date, therapist_id, appointment_time = assigned_slot
                therapist_bookings[(appointment_date, therapist_id)].add(appointment_time)
                patient_dates[patient_id].add(appointment_date)
                appointments.append({
                    "Date": appointment_date,
                    "Time": appointment_time,
                    "Therapist ID": therapist_id,
                    "Patient ID": patient_id,
                    "Status": "Confirmed",
                })

    return pd.DataFrame(appointments), unscheduled_requirements

# 1. Initialize Data in Session State
def load_data():
    if 'patients' not in st.session_state:
        st.session_state.patients = pd.read_csv("Patient_Therapy_Scheduling_Data_1000.csv")
    if 'therapists' not in st.session_state:
        st.session_state.therapists = pd.read_csv("Therapist_Master_Data_25.csv")
    if 'absences' not in st.session_state:
        st.session_state.absences = pd.DataFrame(columns=["Type", "ID", "Date", "Reason"])
    elif "Type" not in st.session_state.absences.columns:
        st.session_state.absences = st.session_state.absences.rename(columns={"Therapist ID": "ID"})
        st.session_state.absences.insert(0, "Type", "Therapist")
    schedule_start_date = datetime.today().date()
    if (
        st.session_state.get("schedule_version") != 2
        or st.session_state.get("schedule_start_date") != schedule_start_date
    ):
        st.session_state.schedule, st.session_state.unscheduled_requirements = build_two_week_schedule(
            st.session_state.patients,
            st.session_state.therapists,
            schedule_start_date,
        )
        st.session_state.schedule_version = 2
        st.session_state.schedule_start_date = schedule_start_date

load_data()

st.title("Therapy Scheduling Manager POC")

# 2. Setup Navigation Tabs
tab1, tab2, tab3 = st.tabs(["View & Edit Schedule", "Mark Absenteeism & Fill Slots", "Add New Patient"])

# --- TAB 1: View & Edit Schedule ---
with tab1:
    st.subheader("Current Schedule")

    if "calendar_date" not in st.session_state:
        st.session_state.calendar_date = datetime.today().date()

    navigation_col1, navigation_col2, navigation_col3, navigation_col4 = st.columns([1, 1, 1, 3])
    with navigation_col1:
        if st.button("Previous week", key="previous_week"):
            st.session_state.calendar_date -= timedelta(days=7)
    with navigation_col2:
        if st.button("Today", key="current_week"):
            st.session_state.calendar_date = datetime.today().date()
    with navigation_col3:
        if st.button("Next week", key="next_week"):
            st.session_state.calendar_date += timedelta(days=7)
    with navigation_col4:
        selected_date = st.date_input(
            "Choose a date in the week",
            key="calendar_date",
        )

    week_start = selected_date - timedelta(days=selected_date.weekday())
    week_dates = [week_start + timedelta(days=offset) for offset in range(7)]
    calendar_schedule = st.session_state.schedule.copy()
    calendar_schedule["Date"] = pd.to_datetime(calendar_schedule["Date"]).dt.date

    st.caption(f"Week of {week_start.strftime('%b %d, %Y')}")
    schedule_end_date = st.session_state.schedule_start_date + timedelta(days=13)
    st.caption(
        f"Two-week plan: {st.session_state.schedule_start_date.strftime('%b %d')} "
        f"to {schedule_end_date.strftime('%b %d, %Y')} | "
        f"{len(calendar_schedule)} appointments scheduled"
    )
    if st.session_state.unscheduled_requirements:
        st.warning(
            f"{st.session_state.unscheduled_requirements} requested sessions could not fit "
            "within therapist capacity for this two-week window."
        )

    calendar_columns = st.columns(7)
    for calendar_column, calendar_date in zip(calendar_columns, week_dates):
        with calendar_column:
            is_today = calendar_date == datetime.today().date()
            day_label = calendar_date.strftime("%a").upper()
            date_label = calendar_date.strftime("%b %d")
            heading_color = "#ff4b4b" if is_today else "#f1f3f5"
            st.markdown(
                f"<div style='border-bottom: 2px solid {heading_color}; padding-bottom: 0.55rem; "
                f"margin-bottom: 0.75rem;'><strong>{day_label}</strong><br>"
                f"<span style='color: {heading_color};'>{date_label}</span></div>",
                unsafe_allow_html=True,
            )

            day_slots = calendar_schedule[calendar_schedule["Date"] == calendar_date]
            if day_slots.empty:
                st.caption("No appointments")
                continue

            day_slots = day_slots.assign(
                _sort_time=pd.to_datetime(day_slots["Time"], format="%I:%M %p").dt.time
            )
            for _, slot in day_slots.sort_values("_sort_time").iterrows():
                status = str(slot["Status"])
                status_color = "#ff4b4b" if "Cancelled" in status else "#21c354"
                st.markdown(
                    f"<div style='border-left: 3px solid {status_color}; padding: 0.45rem 0.5rem; "
                    f"margin-bottom: 0.55rem; background: rgba(255,255,255,0.04);'>"
                    f"<strong>{escape(str(slot['Time']))}</strong><br>"
                    f"<span>{escape(str(slot['Patient ID']))}</span><br>"
                    f"<small>{escape(str(slot['Therapist ID']))}</small><br>"
                    f"<small style='color: {status_color};'>{escape(status)}</small></div>",
                    unsafe_allow_html=True,
                )

# --- TAB 2: Mark Absence & Reassign Cancelled Slots ---
with tab2:
    st.subheader("Mark Patient or Therapist Absence")

    col1, col2, col3 = st.columns(3)
    with col1:
        absence_type = st.radio("Absent person", ["Patient", "Therapist"], horizontal=True)
    with col2:
        if absence_type == "Patient":
            absent_person = st.selectbox("Select Patient", st.session_state.patients["Patient ID"])
        else:
            absent_person = st.selectbox("Select Therapist", st.session_state.therapists["Therapist ID"])
    with col3:
        absent_date = st.date_input("Absence Date", datetime.today(), key="absence_date")

    if st.button("Mark Absent"):
        new_absence = pd.DataFrame([{
            "Type": absence_type,
            "ID": absent_person,
            "Date": absent_date,
            "Reason": "Marked Absent",
        }])
        st.session_state.absences = pd.concat([st.session_state.absences, new_absence], ignore_index=True)
        schedule_date = pd.to_datetime(st.session_state.schedule["Date"]).dt.date
        schedule_column = "Patient ID" if absence_type == "Patient" else "Therapist ID"
        mask = (st.session_state.schedule[schedule_column] == absent_person) & (schedule_date == absent_date)
        st.session_state.schedule.loc[mask, "Status"] = f"Cancelled - {absence_type} Absent"
        st.success(f"{absent_person} marked absent for {absent_date}.")

    st.divider()
    st.subheader("Reassign Cancelled Slots")
    cancelled_slots = st.session_state.schedule[
        st.session_state.schedule["Status"].astype(str).str.startswith("Cancelled -")
    ]

    if not cancelled_slots.empty:
        absence_records = st.session_state.absences.copy()
        absence_records["Date"] = pd.to_datetime(absence_records["Date"]).dt.date

        for index, row in cancelled_slots.iterrows():
            slot_date = pd.to_datetime(row["Date"]).date()
            scheduled_patient_ids = st.session_state.schedule.loc[
                (pd.to_datetime(st.session_state.schedule["Date"]).dt.date == slot_date)
                & (st.session_state.schedule.index != index),
                "Patient ID",
            ].tolist()
            absent_patient_ids = absence_records.loc[
                (absence_records["Type"] == "Patient")
                & (absence_records["Date"] == slot_date),
                "ID",
            ].tolist()
            potential_patients = st.session_state.patients[
                ~st.session_state.patients["Patient ID"].isin(scheduled_patient_ids + absent_patient_ids)
            ]

            absent_therapist_ids = absence_records.loc[
                (absence_records["Type"] == "Therapist")
                & (absence_records["Date"] == slot_date),
                "ID",
            ].tolist()
            available_therapists = [
                therapist_id for therapist_id in st.session_state.therapists["Therapist ID"]
                if therapist_id not in absent_therapist_ids
            ]

            st.markdown(f"**{row['Time']} on {slot_date.strftime('%b %d')}**")
            st.caption(f"Cancelled: {row['Patient ID']} with {row['Therapist ID']}")
            if potential_patients.empty:
                st.warning("No available patients found for this slot.")
                continue

            replacement_patient = st.selectbox(
                "Next available patient",
                potential_patients["Patient ID"].tolist(),
                key=f"replacement_patient_{index}",
            )
            therapist_options = available_therapists or [row["Therapist ID"]]
            replacement_therapist = st.selectbox(
                "Therapist for reassigned slot",
                therapist_options,
                index=therapist_options.index(row["Therapist ID"])
                if row["Therapist ID"] in therapist_options else 0,
                key=f"replacement_therapist_{index}",
            )
            if st.button("Reassign Slot", key=f"reassign_{index}"):
                st.session_state.schedule.loc[index, "Patient ID"] = replacement_patient
                st.session_state.schedule.loc[index, "Therapist ID"] = replacement_therapist
                st.session_state.schedule.loc[index, "Status"] = "Reassigned"
                st.success(f"Slot reassigned to {replacement_patient} with {replacement_therapist}.")
    else:
        st.info("No cancelled slots require reassignment at this time.")

# --- TAB 3: Add New Patient ---
with tab3:
    st.subheader("Patient Intake Form")
    
    with st.form("add_patient_form"):
        new_id = st.text_input("Patient ID (e.g., PAT-1001)")
        scenario = st.selectbox("Scenario Description", [
            "Low-Frequency Maintenance", "Intensive Multi-Disciplinary", "Dual Equal Frequency"
        ])
        
        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Session Start Date")
        with col2:
            end_date = st.date_input("Session End Date", datetime.today() + timedelta(days=90))
            
        therapy_a = st.text_input("Therapy A Requirement (e.g., Speech (2x / week))")
        assigned_therapists = st.text_input("Assigned Therapists (comma separated)")
        
        submit_patient = st.form_submit_button("Add Patient")
        
        if submit_patient:
            new_patient = pd.DataFrame([{
                "Patient ID": new_id,
                "Scenario Description": scenario,
                "Therapy A (Frequency)": therapy_a,
                "Therapy B (Frequency)": "N/A",
                "Therapy C (Frequency)": "N/A",
                "Therapy D (Frequency)": "N/A",
                "Assigned Therapists": assigned_therapists,
                "Start Date": start_date,
                "End Date": end_date
            }])
            st.session_state.patients = pd.concat([st.session_state.patients, new_patient], ignore_index=True)
            st.success(f"Patient {new_id} added successfully.")

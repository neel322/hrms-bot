import random
import sys
from datetime import date, datetime, timedelta
from app.db import (
    Attendance,
    AuthCredentials,
    ChatHistory,
    ChatSession,
    Employee,
    Holiday,
    Invite,
    LeaveBalance,
    LeaveRequest,
    Payroll,
    PerformanceReview,
    get_session,
    init_db,
)
from app.utils import add_months, get_current_date, get_current_datetime


def seed_credentials(session=None):
    """Creates demo credentials for all employees in the database."""
    close_session = False
    if session is None:
        session = get_session()
        close_session = True
    try:
        import hashlib
        import os
        import secrets
        from app.auth import hash_password
        from app.config import get_settings

        settings = get_settings()
        demo_pwd = (settings.demo_password or os.getenv("DEMO_PASSWORD") or "AcmeSecret2026!").strip()

        # Ensure terminated employee ACM-031 exists for access control verification
        terminated_emp = session.query(Employee).filter(Employee.emp_code == "ACM-031").first()
        if not terminated_emp:
            terminated_emp = Employee(
                emp_code="ACM-031",
                name="Karan Johar",
                email="karan.johar@acmetech.com",
                department="Engineering",
                designation="Former Full-Stack Engineer",
                role="employee",
                join_date=date(2021, 5, 1),
                work_location="Headquarters - Bangalore",
                status="terminated",
                manager_id=5,
            )
            session.add(terminated_emp)
            session.commit()
            session.refresh(terminated_emp)

        # Ensure demo new joiner ACM-032 "Rohit Kumar" exists with status 'onboarding', invite pending
        new_joiner = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
        if not new_joiner:
            today = get_current_date()
            prob_end = add_months(today, 6)
            new_joiner = Employee(
                emp_code="ACM-032",
                name="Rohit Kumar",
                email="rohit.kumar@acmetech.com",
                department="Engineering",
                designation="Junior Software Engineer",
                role="employee",
                join_date=today,
                work_location="Headquarters - Bangalore",
                status="onboarding",
                probation_end_date=prob_end,
                manager_id=5,  # Reports to Rahul Mehta
            )
            session.add(new_joiner)
            session.commit()
            session.refresh(new_joiner)

            # Create prorated leave balances for Rohit Kumar
            months_rem = max(1, 12 - today.month + 1)
            for lt, base_days in [("Casual", 12), ("Sick", 10), ("Earned", 18)]:
                p_days = max(1, round(base_days * months_rem / 12))
                session.add(LeaveBalance(
                    employee_id=new_joiner.id,
                    leave_type=lt,
                    total=p_days,
                    used=0,
                    remaining=p_days,
                    year=today.year,
                ))

            # Create pending invite for Rohit
            raw_token = "rohit-demo-invite-token-2026"
            tok_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            session.add(Invite(
                employee_id=new_joiner.id,
                token_hash=tok_hash,
                expires_at=datetime.utcnow() + timedelta(hours=72),
                used_at=None,
            ))
            session.commit()
            print(f"Created demo new joiner Rohit Kumar (ACM-032) with pending invite token: {raw_token}")

        employees = session.query(Employee).all()
        created_count = 0
        for emp in employees:
            creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
            if not creds:
                if settings.demo_password or os.getenv("DEMO_PASSWORD"):
                    pwd = demo_pwd
                else:
                    pwd = secrets.token_urlsafe(10)
                    print(f"Generated credential for {emp.email}: {pwd}")
                creds = AuthCredentials(
                    employee_id=emp.id,
                    password_hash=hash_password(pwd),
                    must_change_password=False,
                    failed_attempts=0,
                )
                session.add(creds)
                created_count += 1
        session.commit()
        if created_count > 0:
            print(f"Seeded credentials for {created_count} employees.")

        print("\n" + "=" * 76)
        print("ACME TECHNOLOGIES HRMS - DEMO CREDENTIALS")
        print(f"Default Password for all seeded personas: {demo_pwd}")
        print("-" * 76)
        print(f"{'Persona':<18} {'Emp Code':<10} {'Email':<30} {'Role':<14}")
        print("-" * 76)
        demo_personas = [
            ("Vikram Malhotra", "ACM-001", "vikram.malhotra@acmetech.com", "manager (CEO)"),
            ("Anita Desai",     "ACM-002", "anita.desai@acmetech.com",     "hr_admin"),
            ("Rahul Mehta",     "ACM-005", "rahul.mehta@acmetech.com",     "manager (VP Eng)"),
            ("Priya Sharma",    "ACM-008", "priya.sharma@acmetech.com",    "employee (SWE)"),
            ("Rohit Kumar",     "ACM-032", "rohit.kumar@acmetech.com",     "employee (New Joiner)"),
            ("Karan Johar",     "ACM-031", "karan.johar@acmetech.com",     "employee (Terminated)"),
        ]
        for name, code, em, ro in demo_personas:
            print(f"{name:<18} {code:<10} {em:<30} {ro:<14}")
        print("=" * 76 + "\n")
    finally:
        if close_session:
            session.close()


def reset_passwords(session=None) -> int:
    """Sets every ACTIVE employee's password to the shared demo password.

    Sets must_change_password=False, failed_attempts=0, locked_until=None,
    and increments token_version (invalidating active JWT sessions).
    Does NOT touch employees whose status is 'onboarding' or 'terminated'.
    Clears caller/auth caches and prints the updated account list.
    """
    import os
    from app.auth import hash_password, invalidate_auth_cache
    from app.config import get_settings

    settings = get_settings()
    demo_pwd = (settings.demo_password or os.getenv("DEMO_PASSWORD") or "").strip()
    if not demo_pwd:
        raise ValueError(
            "DEMO_PASSWORD is empty or not configured. "
            "Please set DEMO_PASSWORD in your .env file or environment (e.g. DEMO_PASSWORD=AcmeSecret2026!) to use --reset-passwords."
        )

    close_session = False
    if session is None:
        session = get_session()
        close_session = True

    try:
        active_employees = (
            session.query(Employee)
            .filter(Employee.status == "active")
            .order_by(Employee.id)
            .all()
        )

        hashed = hash_password(demo_pwd)
        updated_count = 0

        for emp in active_employees:
            creds = session.query(AuthCredentials).filter(AuthCredentials.employee_id == emp.id).first()
            if creds:
                creds.password_hash = hashed
                creds.must_change_password = False
                creds.failed_attempts = 0
                creds.locked_until = None
                creds.token_version = (creds.token_version or 1) + 1
            else:
                creds = AuthCredentials(
                    employee_id=emp.id,
                    password_hash=hashed,
                    must_change_password=False,
                    failed_attempts=0,
                    locked_until=None,
                    token_version=2,
                )
                session.add(creds)
            updated_count += 1

        session.commit()

        # Invalidate all auth/caller caches
        invalidate_auth_cache()

        print(f"\nReset passwords for {updated_count} active employees.")
        print("=" * 76)
        print("ACME TECHNOLOGIES HRMS - RESET PASSWORDS (ACTIVE ACCOUNTS)")
        print(f"Shared Demo Password: {demo_pwd}")
        print("-" * 76)
        print(f"{'Name':<24} {'Email':<32} {'Role':<14}")
        print("-" * 76)
        for emp in active_employees:
            print(f"{emp.name:<24} {emp.email:<32} {emp.role:<14}")
        print("=" * 76 + "\n")
        return updated_count
    finally:
        if close_session:
            session.close()


def seed_database(reset: bool = False):
    """Seeds realistic synthetic HRMS data for Acme Technologies.
    If reset=True, clears all tables and re-seeds from scratch.
    """
    init_db()
    session = get_session()

    try:
        if reset:
            print("Reset requested. Clearing existing database records...")
            session.query(ChatHistory).delete()
            session.query(ChatSession).delete()
            session.query(PerformanceReview).delete()
            session.query(Payroll).delete()
            session.query(Attendance).delete()
            session.query(LeaveRequest).delete()
            session.query(LeaveBalance).delete()
            session.query(Invite).delete()
            session.query(AuthCredentials).delete()
            session.query(Holiday).delete()
            session.query(Employee).delete()
            session.commit()
            print("Existing records cleared. Re-seeding tables...")
        elif session.query(Employee).count() > 0:
            print("Database already contains employees. Ensuring credentials...")
            seed_credentials(session)
            return

        today = get_current_date()

        # 1. Define Employees
        # We need realistic 30 employees with clear hierarchy
        # Demo Personas:
        # Priya Sharma: Software Engineer, Engineering (reports to Rahul Mehta)
        # Rahul Mehta: Engineering Manager, Engineering (reports to CEO)
        # Anita Desai: HR Lead / HR Admin, HR (reports to CEO)

        employees_data = [
            # Executive Leadership
            {"emp_code": "ACM-001", "name": "Vikram Malhotra", "email": "vikram.malhotra@acmetech.com", "department": "Executive", "designation": "Chief Executive Officer", "role": "manager", "join_date": date(2020, 1, 15), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": None},
            
            # HR Department
            {"emp_code": "ACM-002", "name": "Anita Desai", "email": "anita.desai@acmetech.com", "department": "HR", "designation": "HR Director & Operations Lead", "role": "hr_admin", "join_date": date(2020, 3, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 1},
            {"emp_code": "ACM-003", "name": "Kavita Rao", "email": "kavita.rao@acmetech.com", "department": "HR", "designation": "HR Generalist", "role": "hr_admin", "join_date": date(2021, 6, 15), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 2},
            {"emp_code": "ACM-004", "name": "Deepak Verma", "email": "deepak.verma@acmetech.com", "department": "HR", "designation": "Talent Acquisition Specialist", "role": "employee", "join_date": date(2022, 2, 10), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 2},

            # Engineering Department - Managers
            {"emp_code": "ACM-005", "name": "Rahul Mehta", "email": "rahul.mehta@acmetech.com", "department": "Engineering", "designation": "VP of Engineering", "role": "manager", "join_date": date(2020, 2, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 1},
            {"emp_code": "ACM-006", "name": "Siddharth Nair", "email": "siddharth.nair@acmetech.com", "department": "Engineering", "designation": "Engineering Manager - Cloud", "role": "manager", "join_date": date(2021, 1, 10), "work_location": "Pune Tech Hub", "status": "active", "manager_id": 5},
            {"emp_code": "ACM-007", "name": "Sneha Kulkarni", "email": "sneha.kulkarni@acmetech.com", "department": "Engineering", "designation": "QA Lead", "role": "manager", "join_date": date(2021, 4, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 5},

            # Engineering Department - ICs
            {"emp_code": "ACM-008", "name": "Priya Sharma", "email": "priya.sharma@acmetech.com", "department": "Engineering", "designation": "Senior Full-Stack Engineer", "role": "employee", "join_date": date(2021, 8, 16), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 5},
            {"emp_code": "ACM-009", "name": "Arjun Patel", "email": "arjun.patel@acmetech.com", "department": "Engineering", "designation": "Backend Software Engineer", "role": "employee", "join_date": date(2022, 5, 2), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 5},
            {"emp_code": "ACM-010", "name": "Neha Gupta", "email": "neha.gupta@acmetech.com", "department": "Engineering", "designation": "Frontend Engineer", "role": "employee", "join_date": date(2022, 9, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 5},
            {"emp_code": "ACM-011", "name": "Rohan Joshi", "email": "rohan.joshi@acmetech.com", "department": "Engineering", "designation": "DevOps Engineer", "role": "employee", "join_date": date(2022, 11, 14), "work_location": "Pune Tech Hub", "status": "active", "manager_id": 6},
            {"emp_code": "ACM-012", "name": "Ananya Iyer", "email": "ananya.iyer@acmetech.com", "department": "Engineering", "designation": "Cloud Infrastructure Engineer", "role": "employee", "join_date": date(2023, 2, 20), "work_location": "Pune Tech Hub", "status": "active", "manager_id": 6},
            {"emp_code": "ACM-013", "name": "Aakash Banerjee", "email": "aakash.banerjee@acmetech.com", "department": "Engineering", "designation": "QA Automation Engineer", "role": "employee", "join_date": date(2023, 4, 10), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 7},
            {"emp_code": "ACM-014", "name": "Divya Reddy", "email": "divya.reddy@acmetech.com", "department": "Engineering", "designation": "Junior QA Analyst", "role": "employee", "join_date": date(2023, 7, 3), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 7},

            # Sales Department
            {"emp_code": "ACM-015", "name": "Manish Kapoor", "email": "manish.kapoor@acmetech.com", "department": "Sales", "designation": "Director of Global Sales", "role": "manager", "join_date": date(2020, 6, 1), "work_location": "Mumbai Commercial Center", "status": "active", "manager_id": 1},
            {"emp_code": "ACM-016", "name": "Pooja Hegde", "email": "pooja.hegde@acmetech.com", "department": "Sales", "designation": "Enterprise Account Executive", "role": "employee", "join_date": date(2021, 9, 1), "work_location": "Mumbai Commercial Center", "status": "active", "manager_id": 15},
            {"emp_code": "ACM-017", "name": "Varun Saxena", "email": "varun.saxena@acmetech.com", "department": "Sales", "designation": "Senior Sales Representative", "role": "employee", "join_date": date(2022, 1, 10), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 15},
            {"emp_code": "ACM-018", "name": "Shreya Das", "email": "shreya.das@acmetech.com", "department": "Sales", "designation": "Business Development Specialist", "role": "employee", "join_date": date(2022, 8, 15), "work_location": "Mumbai Commercial Center", "status": "active", "manager_id": 15},
            {"emp_code": "ACM-019", "name": "Aditya Nambiar", "email": "aditya.nambiar@acmetech.com", "department": "Sales", "designation": "Inbound Sales Associate", "role": "employee", "join_date": date(2023, 3, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 15},

            # Finance Department
            {"emp_code": "ACM-020", "name": "Suresh Singhania", "email": "suresh.singhania@acmetech.com", "department": "Finance", "designation": "Head of Finance & Accounting", "role": "manager", "join_date": date(2020, 4, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 1},
            {"emp_code": "ACM-021", "name": "Meera Chawla", "email": "meera.chawla@acmetech.com", "department": "Finance", "designation": "Senior Financial Analyst", "role": "employee", "join_date": date(2021, 10, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 20},
            {"emp_code": "ACM-022", "name": "Karthik Pillai", "email": "karthik.pillai@acmetech.com", "department": "Finance", "designation": "Payroll Specialist", "role": "employee", "join_date": date(2022, 4, 18), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 20},
            {"emp_code": "ACM-023", "name": "Tanvi Bhatia", "email": "tanvi.bhatia@acmetech.com", "department": "Finance", "designation": "Accounts Payable Associate", "role": "employee", "join_date": date(2023, 1, 9), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 20},

            # Customer Support & Operations
            {"emp_code": "ACM-024", "name": "Gaurav Sen", "email": "gaurav.sen@acmetech.com", "department": "Support", "designation": "Customer Success & Support Director", "role": "manager", "join_date": date(2020, 7, 15), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 1},
            {"emp_code": "ACM-025", "name": "Swati Deshmukh", "email": "swati.deshmukh@acmetech.com", "department": "Support", "designation": "Senior Technical Support Specialist", "role": "employee", "join_date": date(2021, 11, 8), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 24},
            {"emp_code": "ACM-026", "name": "Harish Choudhary", "email": "harish.choudhary@acmetech.com", "department": "Support", "designation": "Technical Support Engineer", "role": "employee", "join_date": date(2022, 6, 20), "work_location": "Pune Tech Hub", "status": "active", "manager_id": 24},
            {"emp_code": "ACM-027", "name": "Ishaan Roy", "email": "ishaan.roy@acmetech.com", "department": "Support", "designation": "Tier 1 Helpdesk Specialist", "role": "employee", "join_date": date(2022, 10, 3), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 24},
            {"emp_code": "ACM-028", "name": "Ritu Sengupta", "email": "ritu.sengupta@acmetech.com", "department": "Support", "designation": "Customer Success Manager", "role": "employee", "join_date": date(2023, 5, 15), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 24},
            
            # Product & Design
            {"emp_code": "ACM-029", "name": "Nikhil Agarwal", "email": "nikhil.agarwal@acmetech.com", "department": "Engineering", "designation": "Principal Product Manager", "role": "manager", "join_date": date(2021, 2, 1), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 1},
            {"emp_code": "ACM-030", "name": "Pallavi Menon", "email": "pallavi.menon@acmetech.com", "department": "Engineering", "designation": "UI/UX Product Designer", "role": "employee", "join_date": date(2022, 3, 14), "work_location": "Headquarters - Bangalore", "status": "active", "manager_id": 29},
        ]

        employee_objects = []
        for emp_info in employees_data:
            emp = Employee(**emp_info)
            session.add(emp)
            employee_objects.append(emp)
        session.commit()

        # Fetch all employees with assigned auto-increment IDs
        all_emps = session.query(Employee).all()
        emp_map = {e.emp_code: e for e in all_emps}

        # 2. Seed Holidays (12 standard holidays for the calendar year)
        current_year = today.year
        holidays_data = [
            (date(current_year, 1, 1), "New Year's Day", "All"),
            (date(current_year, 1, 26), "Republic Day", "All"),
            (date(current_year, 3, 25), "Holi", "All"),
            (date(current_year, 4, 11), "Eid al-Fitr", "All"),
            (date(current_year, 5, 1), "May Day / Labour Day", "All"),
            (date(current_year, 8, 15), "Independence Day", "All"),
            (date(current_year, 9, 7), "Ganesh Chaturthi", "All"),
            (date(current_year, 10, 2), "Gandhi Jayanti", "All"),
            (date(current_year, 10, 12), "Dussehra", "All"),
            (date(current_year, 11, 1), "Diwali (Deepavali)", "All"),
            (date(current_year, 11, 15), "Guru Nanak Jayanti", "All"),
            (date(current_year, 12, 25), "Christmas Day", "All"),
        ]
        for h_date, h_name, h_region in holidays_data:
            session.add(Holiday(date=h_date, name=h_name, region=h_region))
        session.commit()

        # 3. Seed Leave Balances for everyone
        # Casual: 12 total, Sick: 10 total, Earned: 18 total, Maternity/Paternity: 15/180 total
        random.seed(42)
        for emp in all_emps:
            # Casual Leave
            cl_used = random.randint(1, 6)
            session.add(LeaveBalance(
                employee_id=emp.id,
                leave_type="Casual",
                total=12,
                used=cl_used,
                remaining=12 - cl_used,
                year=current_year,
            ))

            # Sick Leave
            sl_used = random.randint(0, 4)
            session.add(LeaveBalance(
                employee_id=emp.id,
                leave_type="Sick",
                total=10,
                used=sl_used,
                remaining=10 - sl_used,
                year=current_year,
            ))

            # Earned Leave
            el_used = random.randint(2, 8)
            session.add(LeaveBalance(
                employee_id=emp.id,
                leave_type="Earned",
                total=18,
                used=el_used,
                remaining=18 - el_used,
                year=current_year,
            ))

            # Maternity / Paternity
            parental_total = 180 if "a" in emp.name.lower() and emp.id in [2, 7, 8, 10, 14, 21, 28, 30] else 15
            session.add(LeaveBalance(
                employee_id=emp.id,
                leave_type="Maternity/Paternity",
                total=parental_total,
                used=0,
                remaining=parental_total,
                year=current_year,
            ))
        session.commit()

        # 4. Seed 60+ Leave Requests
        # Needs mixed statuses: approved, rejected, pending, including future leaves (tomorrow, next week)
        leave_reasons = [
            "Family gathering and wedding ceremony",
            "Suffering from viral fever and throat infection",
            "Personal domestic work and bank visits",
            "Doctor appointment and health checkup",
            "Vacation travel with family",
            "Urgent personal matter at hometown",
            "Child school annual function and parent-teacher meeting",
            "Home renovation supervision",
            "Attending cousin's engagement",
            "Seasonal flu recovery",
        ]

        # Specific prominent requests for demo personas
        priya = emp_map["ACM-008"]
        rahul = emp_map["ACM-005"]
        anita = emp_map["ACM-002"]

        # Priya: 1 pending for next week, 1 approved in past, 1 rejected
        next_week_mon = today + timedelta(days=(7 - today.weekday()))
        tomorrow = today + timedelta(days=1)

        custom_requests = [
            LeaveRequest(
                employee_id=priya.id,
                leave_type="Casual",
                start_date=tomorrow,
                end_date=tomorrow,
                days=1,
                status="pending",
                reason="Personal work at registrar office",
                approver_id=rahul.id,
            ),
            LeaveRequest(
                employee_id=priya.id,
                leave_type="Earned",
                start_date=next_week_mon,
                end_date=next_week_mon + timedelta(days=3),
                days=4,
                status="pending",
                reason="Family trip to Manali",
                approver_id=rahul.id,
            ),
            LeaveRequest(
                employee_id=priya.id,
                leave_type="Sick",
                start_date=today - timedelta(days=20),
                end_date=today - timedelta(days=19),
                days=2,
                status="approved",
                reason="Viral fever and rest advised by physician",
                approver_id=rahul.id,
            ),
            LeaveRequest(
                employee_id=priya.id,
                leave_type="Casual",
                start_date=today - timedelta(days=40),
                end_date=today - timedelta(days=39),
                days=2,
                status="rejected",
                reason="Sprint release freeze window",
                approver_id=rahul.id,
            ),
        ]
        for req in custom_requests:
            session.add(req)

        # Generate ~65 more leave requests across other employees
        leave_types = ["Casual", "Sick", "Earned"]
        statuses = ["approved", "approved", "approved", "rejected", "pending"]

        req_count = len(custom_requests)
        for emp in all_emps:
            if req_count >= 70:
                break
            # 2 to 3 requests per employee
            for i in range(random.randint(2, 3)):
                days_span = random.randint(1, 4)
                # Some in past, some tomorrow/next week
                is_future = (random.random() < 0.25)
                if is_future:
                    offset = random.randint(1, 14)
                    req_start = today + timedelta(days=offset)
                    status_choice = "pending" if offset <= 7 else random.choice(["pending", "approved"])
                else:
                    offset = random.randint(5, 55)
                    req_start = today - timedelta(days=offset)
                    status_choice = random.choice(["approved", "approved", "rejected"])

                req_end = req_start + timedelta(days=days_span - 1)
                approver_id = emp.manager_id if emp.manager_id else 1

                session.add(LeaveRequest(
                    employee_id=emp.id,
                    leave_type=random.choice(leave_types),
                    start_date=req_start,
                    end_date=req_end,
                    days=days_span,
                    status=status_choice,
                    reason=random.choice(leave_reasons),
                    approver_id=approver_id,
                ))
                req_count += 1

        session.commit()

        # 5. Seed Attendance for 60 past days for all employees
        # status: present, absent, wfh, leave, holiday
        holiday_dates = {h[0] for h in holidays_data}
        attendance_records = []

        for day_offset in range(60, 0, -1):
            curr_date = today - timedelta(days=day_offset)
            is_weekend = curr_date.weekday() >= 5
            is_holiday = curr_date in holiday_dates

            for emp in all_emps:
                if is_weekend:
                    continue
                elif is_holiday:
                    attendance_records.append(Attendance(
                        employee_id=emp.id,
                        date=curr_date,
                        check_in=None,
                        check_out=None,
                        status="holiday",
                    ))
                else:
                    # Realistic distribution: 75% present, 18% wfh, 4% leave, 3% absent
                    r = random.random()
                    if r < 0.75:
                        in_hour = random.randint(9, 10)
                        in_min = random.randint(0, 45)
                        out_hour = random.randint(18, 20)
                        out_min = random.randint(0, 50)
                        attendance_records.append(Attendance(
                            employee_id=emp.id,
                            date=curr_date,
                            check_in=f"{in_hour:02d}:{in_min:02d}",
                            check_out=f"{out_hour:02d}:{out_min:02d}",
                            status="present",
                        ))
                    elif r < 0.93:
                        in_hour = random.randint(9, 10)
                        in_min = random.randint(0, 30)
                        out_hour = random.randint(18, 19)
                        out_min = random.randint(30, 59)
                        attendance_records.append(Attendance(
                            employee_id=emp.id,
                            date=curr_date,
                            check_in=f"{in_hour:02d}:{in_min:02d}",
                            check_out=f"{out_hour:02d}:{out_min:02d}",
                            status="wfh",
                        ))
                    elif r < 0.97:
                        attendance_records.append(Attendance(
                            employee_id=emp.id,
                            date=curr_date,
                            check_in=None,
                            check_out=None,
                            status="leave",
                        ))
                    else:
                        attendance_records.append(Attendance(
                            employee_id=emp.id,
                            date=curr_date,
                            check_in=None,
                            check_out=None,
                            status="absent",
                        ))

        session.bulk_save_objects(attendance_records)
        session.commit()

        # 6. Seed Payroll (3 months)
        # Months: Last 3 complete months relative to current date
        # E.g., if today is month M, months M-3, M-2, M-1
        months_to_seed = []
        target_m = today.month
        target_y = today.year
        for _ in range(3):
            target_m -= 1
            if target_m <= 0:
                target_m = 12
                target_y -= 1
            months_to_seed.append((target_m, target_y))

        salary_bases = {
            "Executive": 250000.0,
            "manager": 160000.0,
            "hr_admin": 110000.0,
            "employee": 85000.0,
        }

        for m, y in months_to_seed:
            for emp in all_emps:
                base_val = salary_bases.get(emp.role, 85000.0)
                if "Senior" in emp.designation or "Lead" in emp.designation:
                    base_val += 30000.0
                elif "Junior" in emp.designation:
                    base_val -= 25000.0

                basic = round(base_val * 0.50, 2)
                hra = round(basic * 0.40, 2)
                allowances = round(base_val * 0.20, 2)
                pf_deduction = round(basic * 0.12, 2)
                tax_deduction = round(base_val * 0.10, 2)
                deductions = round(pf_deduction + tax_deduction, 2)
                net_pay = round(basic + hra + allowances - deductions, 2)

                session.add(Payroll(
                    employee_id=emp.id,
                    month=m,
                    year=y,
                    basic=basic,
                    hra=hra,
                    allowances=allowances,
                    deductions=deductions,
                    net_pay=net_pay,
                    payslip_status="generated",
                ))
        session.commit()

        # 7. Seed Performance Reviews (1 per person)
        cycles = ["FY25 Annual Review", "Q2 2025 Performance Review"]
        review_comments = [
            "Consistently exceeds expectations in project delivery and exhibits stellar teamwork.",
            "Strong technical contribution; recommended to take more leadership in architecture discussions.",
            "Great problem solver, demonstrates high ownership and excellent customer alignment.",
            "Solid performance, meets all quarterly milestones; should mentor junior peers next cycle.",
            "Outstanding domain expertise and proactive communication across cross-functional teams.",
            "Reliable and methodical execution; has significantly improved sprint velocity.",
        ]

        for emp in all_emps:
            reviewer_id = emp.manager_id if emp.manager_id else emp.id
            rating = round(random.choice([3.8, 4.0, 4.2, 4.5, 4.8, 5.0]), 1)
            # Special high rating for Priya
            if emp.emp_code == "ACM-008":
                rating = 4.8
                comments = "Priya has shown remarkable technical competence and led the core HRMS architectural overhaul seamlessly."
            else:
                comments = random.choice(review_comments)

            session.add(PerformanceReview(
                employee_id=emp.id,
                cycle="FY25 Annual Review",
                rating=rating,
                reviewer_id=reviewer_id,
                comments=comments,
            ))
        session.commit()

        # 8. Seed sample Chat History
        demo_chat = [
            ChatHistory(
                session_id="session-demo-01",
                user_id="ACM-008",
                role="user",
                content="Hello, what is my remaining casual leave balance for this year?",
                created_at=get_current_datetime() - timedelta(hours=2),
            ),
            ChatHistory(
                session_id="session-demo-01",
                user_id="ACM-008",
                role="assistant",
                content="Hello Priya! According to HRMS records, you have used casual leaves and have your remaining balance updated in the portal.",
                created_at=get_current_datetime() - timedelta(hours=2, minutes=-1),
            ),
        ]
        session.bulk_save_objects(demo_chat)
        session.commit()

        seed_credentials(session)
        print("Successfully seeded HRMS database with realistic data!")

    finally:
        session.close()


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    seed_database(reset=reset_flag)

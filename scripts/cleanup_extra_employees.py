"""Script to clean up test-created employees from the database, keeping only the 32 Acme seed employees."""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import (
    Employee,
    LeaveBalance,
    LeaveRequest,
    Attendance,
    Payroll,
    PerformanceReview,
    Invite,
    AuthCredentials,
    get_session,
)


def cleanup_extra_employees(max_base_id: int = 32):
    session = get_session()
    try:
        # Find extra employees
        extra_emps = session.query(Employee).filter(Employee.id > max_base_id).all()
        # Always ensure seed statuses for ACM-031 (terminated) and ACM-032 (onboarding)
        rohit = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
        if rohit and rohit.status != "onboarding":
            rohit.status = "onboarding"
        karan = session.query(Employee).filter(Employee.emp_code == "ACM-031").first()
        if karan and karan.status != "terminated":
            karan.status = "terminated"
        session.commit()

        extra_ids = [e.id for e in extra_emps]
        if not extra_ids:
            print(f"No extra employees found beyond ID {max_base_id}.")
            return 0

        print(f"Found {len(extra_ids)} extra employees to clean up.")

        # 1. Nullify references from any employee to extra IDs
        session.query(Employee).filter(Employee.manager_id.in_(extra_ids)).update(
            {Employee.manager_id: None}, synchronize_session=False
        )

        # 2. Nullify approvers/reviewers pointing to extra IDs
        session.query(LeaveRequest).filter(LeaveRequest.approver_id.in_(extra_ids)).update(
            {LeaveRequest.approver_id: None}, synchronize_session=False
        )
        session.query(PerformanceReview).filter(PerformanceReview.reviewer_id.in_(extra_ids)).update(
            {PerformanceReview.reviewer_id: None}, synchronize_session=False
        )

        # 3. Delete dependent rows for extra IDs
        lb_count = session.query(LeaveBalance).filter(LeaveBalance.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        lr_count = session.query(LeaveRequest).filter(LeaveRequest.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        att_count = session.query(Attendance).filter(Attendance.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        pay_count = session.query(Payroll).filter(Payroll.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        pr_count = session.query(PerformanceReview).filter(PerformanceReview.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        inv_count = session.query(Invite).filter(Invite.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        cred_count = session.query(AuthCredentials).filter(AuthCredentials.employee_id.in_(extra_ids)).delete(synchronize_session=False)
        # 4. Delete extra employee rows
        emp_count = session.query(Employee).filter(Employee.id.in_(extra_ids)).delete(synchronize_session=False)

        # 5. Ensure seed statuses for ACM-031 (terminated) and ACM-032 (onboarding)
        rohit = session.query(Employee).filter(Employee.emp_code == "ACM-032").first()
        if rohit and rohit.status != "onboarding":
            rohit.status = "onboarding"
        karan = session.query(Employee).filter(Employee.emp_code == "ACM-031").first()
        if karan and karan.status != "terminated":
            karan.status = "terminated"

        session.commit()
        print(f"Cleanup complete:")
        print(f"  - Employees deleted: {emp_count}")
        print(f"  - Credentials deleted: {cred_count}")
        print(f"  - Invites deleted: {inv_count}")
        print(f"  - Leave balances deleted: {lb_count}")
        print(f"  - Leave requests deleted: {lr_count}")
        print(f"  - Attendances deleted: {att_count}")
        print(f"  - Payroll records deleted: {pay_count}")
        print(f"  - Reviews deleted: {pr_count}")
        return emp_count
    except Exception as exc:
        session.rollback()
        print(f"Error during cleanup: {exc}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    cleanup_extra_employees()

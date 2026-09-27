from sqlalchemy import text

from app.exceptions import StudentNotFoundError
from app.services.database import get_connection


def get_student_academic_info(student_id: str) -> dict:
    query = text(
        'SELECT "Student_ID", "Student_Name", "CGPA", "Completed_Hours", "Program_ID" '
        'FROM "Student" WHERE "Student_ID" = :sid'
    )
    with get_connection() as conn:
        row = conn.execute(query, {"sid": student_id}).mappings().first()
    if row is None:
        raise StudentNotFoundError(f"No Student found for Student_ID={student_id}.")
    return dict(row)


def get_student_transcript(student_id: str) -> list[dict]:
    query = text(
        'SELECT t."Course_ID", c."Course_Name", t."Grade", t."Term", t."Section_ID" '
        'FROM "Takes" t JOIN "Course" c ON c."Course_ID" = t."Course_ID" '
        'WHERE t."Student_ID" = :sid ORDER BY t."Term"'
    )
    with get_connection() as conn:
        rows = conn.execute(query, {"sid": student_id}).mappings().all()
    return [dict(row) for row in rows]

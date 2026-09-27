from sqlalchemy import text

from app.exceptions import SectionCourseMismatchError, SectionNotFoundError
from app.services.database import get_connection


def get_section_capacity(section_id: str, course_id: int, term: int) -> dict:
    """Section_ID alone is NOT a unique key in this schema -- the same section number
    (e.g. '61') is reused across different courses and different terms. The real key is
    the (Section_ID, Course_ID, Term) triple, which is exactly what Raise_Capacity stores,
    so the caller must supply all three."""
    query = text(
        'SELECT "Section_ID", "Course_ID", "Term", "Current_Capacity", '
        '"Total_Capacity", "Additional_Capacity" FROM "Section" '
        'WHERE "Section_ID" = :sid AND "Course_ID" = :cid AND "Term" = :term'
    )
    with get_connection() as conn:
        row = conn.execute(query, {"sid": section_id, "cid": course_id, "term": term}).mappings().first()
        if row is not None:
            return dict(row)

        # No exact match -- check whether this section number exists in this term under a
        # DIFFERENT course, so the error can say "wrong course" instead of just "not found".
        other = conn.execute(
            text('SELECT "Course_ID" FROM "Section" WHERE "Section_ID" = :sid AND "Term" = :term'),
            {"sid": section_id, "term": term},
        ).mappings().first()

    if other is not None:
        raise SectionCourseMismatchError(
            f"Section_ID={section_id} in Term={term} belongs to Course_ID={other['Course_ID']}, "
            f"not the requested Course_ID={course_id}."
        )
    raise SectionNotFoundError(
        f"No Section found for Section_ID={section_id}, Course_ID={course_id}, Term={term}."
    )


def evaluate_capacity(capacity_data: dict) -> dict:
    """All values below come directly from the Section row; nothing here is estimated.

    A missing, non-integer, or negative value makes the record invalid: the result is
    unresolved (satisfied=None), which routes to MANUAL_REVIEW rather than APPROVE/REJECT."""
    current_capacity = capacity_data["Current_Capacity"]
    total_capacity = capacity_data["Total_Capacity"]
    additional_capacity = capacity_data["Additional_Capacity"]

    values = (current_capacity, total_capacity, additional_capacity)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
        return {
            "current_capacity": current_capacity,
            "total_capacity": total_capacity,
            "additional_capacity": additional_capacity,
            "maximum_allowed_capacity": None,
            "remaining_raise_capacity": None,
            "satisfied": None,
            "error": "Section capacity record is missing a value or contains a negative/non-integer value.",
        }

    maximum_allowed_capacity = total_capacity + additional_capacity
    remaining_raise_capacity = maximum_allowed_capacity - current_capacity
    extra_capacity_available = current_capacity < maximum_allowed_capacity

    return {
        "current_capacity": current_capacity,
        "total_capacity": total_capacity,
        "additional_capacity": additional_capacity,
        "maximum_allowed_capacity": maximum_allowed_capacity,
        "remaining_raise_capacity": remaining_raise_capacity,
        "satisfied": extra_capacity_available,
    }

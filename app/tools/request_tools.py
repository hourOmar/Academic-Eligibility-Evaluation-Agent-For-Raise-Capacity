from sqlalchemy import text

from app.exceptions import NotRaiseCapacityRequestError, RequestNotFoundError
from app.services.database import get_connection


def get_raise_capacity_request(request_id: str) -> dict:
    """Fetch a Request + Raise_Capacity pair. Raises a specific error for each distinct
    failure mode instead of a generic "not found", per the error-handling requirements."""
    with get_connection() as conn:
        request_row = conn.execute(
            text(
                'SELECT "Request_ID", "Student_ID", "Request_Type", "Request_Date", '
                '"Submission_Time", "Description", "Current_Status" '
                'FROM "Request" WHERE "Request_ID" = :rid'
            ),
            {"rid": request_id},
        ).mappings().first()

        if request_row is None:
            raise RequestNotFoundError(f"No Request found for Request_ID={request_id}.")

        request_type = (request_row["Request_Type"] or "").strip().lower()
        if request_type != "raise capacity":
            raise NotRaiseCapacityRequestError(
                f"Request_ID={request_id} has Request_Type={request_row['Request_Type']!r}, "
                "expected 'Raise Capacity'."
            )

        raise_capacity_row = conn.execute(
            text(
                'SELECT "Reason", "Course_ID", "Section_ID", "Term" '
                'FROM "Raise_Capacity" WHERE "Request_ID" = :rid'
            ),
            {"rid": request_id},
        ).mappings().first()

        if raise_capacity_row is None:
            raise NotRaiseCapacityRequestError(
                f"Request_ID={request_id} is marked Request_Type='Raise Capacity' but has no "
                "matching Raise_Capacity record."
            )

    return {
        "Request_ID": request_row["Request_ID"],
        "Student_ID": request_row["Student_ID"],
        "Request_Type": request_row["Request_Type"],
        "Current_Status": request_row["Current_Status"],
        "Description": request_row["Description"],
        "Reason": raise_capacity_row["Reason"],
        "Course_ID": raise_capacity_row["Course_ID"],
        "Section_ID": raise_capacity_row["Section_ID"],
        "Term": raise_capacity_row["Term"],
    }

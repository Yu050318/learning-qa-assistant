"""Parser metadata preserved across ingestion retries."""
from uuid import uuid4
def prepare_retry_metadata(metadata: dict) -> dict:
    result = dict(metadata)
    restart_codes = {
        "MINERU_SUBMISSION_UNKNOWN", "MINERU_SUBMISSION_FAILED", "MINERU_UPLOAD_FAILED",
        "MINERU_UPLOAD_AUTH_FAILED", "MINERU_UPLOAD_TIMEOUT", "MINERU_RATE_LIMITED",
        "MINERU_UPSTREAM_UNAVAILABLE", "MINERU_PARSE_FAILED", "MINERU_OUTPUT_UNSUPPORTED",
    }
    if result.get("provider") == "mineru" and (
        result.get("error_code") in restart_codes or result.get("phase") == "uploading"
    ):
        result.update(
            generation=int(result.get("generation", 0)) + 1,
            attempt_id=str(uuid4()),
            batch_id=None,
        )
    result.update(phase="validating", failed_phase=None, error_code=None)
    result.pop("retryable", None)
    return result

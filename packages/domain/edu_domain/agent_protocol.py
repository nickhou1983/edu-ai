JOB_ID_HEADER = "X-Edu-Job-Id"
PAGE_READ_BUDGET_HEADER = "X-Edu-Page-Read-Budget"
MAX_AGENT_METADATA_BYTES = 1_250_000
MODEL_TIMEOUT_CODES = frozenset({"model_sdk_timeout", "model_http_timeout", "model_page_deadline"})


def retry_after_seconds(value: str | None) -> int | None:
    if value is not None and value.isascii() and value.isdecimal():
        return min(int(value), 120) if len(value) <= 9 else 120
    return None

from datetime import timezone

def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

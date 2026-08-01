select
    seq, event_id, event_type, source, entity_id,
    event_time, record_time,
    payload, payload_hash,
    is_error_declaration, declaration_time, reason, corrective_event_ids
from raw.event_log

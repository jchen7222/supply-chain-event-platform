-- Recall-drill read model: who holds every synthetic lot right now.
select entity_id as lot,
       json_extract_string(payload, '$.ndc') as ndc,
       json_extract_string(payload, '$.holder_after') as holder,
       event_type as last_event, event_time, record_time
from {{ ref('current_state') }}
where source = 'pharma_synthetic'

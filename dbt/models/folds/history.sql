-- Full version chain per entity: valid_from/valid_to via lead().
with visible as (
    select * from {{ ref('stg_events') }} where not is_error_declaration
)
select
    entity_id, source, event_type, event_time, record_time as valid_from,
    lead(record_time) over (
        partition by entity_id order by record_time, seq) as valid_to,
    payload, payload_hash, seq
from visible

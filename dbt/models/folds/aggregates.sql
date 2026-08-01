select source, event_type,
       count(*) as events,
       count(distinct entity_id) as entities,
       min(record_time) as first_record,
       max(record_time) as last_record
from {{ ref('stg_events') }}
where not is_error_declaration
group by 1, 2

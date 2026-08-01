select
    entity_id,
    json_extract_string(payload, '$.generic_name')  as generic_name,
    json_extract_string(payload, '$.company_name')  as company_name,
    json_extract_string(payload, '$.status')        as status,
    json_extract_string(payload, '$.availability')  as availability,
    json_extract_string(payload, '$.update_date')   as update_date,
    record_time as observed_at
from {{ ref('current_state') }}
where event_type = 'fda_shortage_observed'

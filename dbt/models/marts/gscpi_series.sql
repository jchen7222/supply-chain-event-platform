select
    json_extract_string(payload, '$.observation_date') as period,
    cast(json_extract_string(payload, '$.value') as double) as gscpi,
    record_time as observed_at
from {{ ref('current_state') }}
where event_type = 'index_value_observed'
order by period

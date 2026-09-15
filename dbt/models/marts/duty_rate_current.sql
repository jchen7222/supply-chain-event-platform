-- The duty schedule in force RIGHT NOW: one row per HS code, latest revision.
-- For a past date, build from point_in_time instead — see duty_rate_as_of.sql.
select
    json_extract_string(payload, '$.hts_code')        as hts_code,
    json_extract_string(payload, '$.description')     as description,
    json_extract_string(payload, '$.general_rate_raw') as general_rate_raw,
    cast(json_extract_string(payload, '$.ad_valorem_rate') as double) as ad_valorem_rate,
    json_extract_string(payload, '$.rate_kind')       as rate_kind,
    event_time                                        as effective_from
from {{ ref('current_state') }}
where event_type = 'duty_rate_observed'
order by hts_code
